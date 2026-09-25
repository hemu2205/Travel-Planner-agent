import os
import sys
import re
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Ensure parent directory and root are in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from callback_logging import log_query_to_model, log_model_response
from adk_utils.plugins import Graceful429Plugin

import google.cloud.logging
from google.adk import Agent
from google.adk.models import Gemini
from google.genai import types
from google.adk.tools.tool_context import ToolContext
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps.app import App

# Import modular tools
from .tools.flights import search_flights, select_seat, check_flight_status
from .tools.hotels import search_hotels, select_room
from .tools.memory import memorize, recall

# Load environment variables
load_dotenv()

# Setup logging with local fallback
try:
    cloud_logging_client = google.cloud.logging.Client()
    cloud_logging_client.setup_logging()
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

MODEL_NAME = os.getenv("MODEL", "gemini-3.6-flash")
RETRY_OPTIONS = types.HttpRetryOptions(initial_delay=1, max_delay=3, attempts=30)


# ==========================================
# Session State Initialization Callback
# ==========================================

def _sync_destination_session_title(callback_context: CallbackContext) -> None:
    """Ensures the chat session's display name reflects the destination place."""
    dest = callback_context.state.get("destination")
    if dest and str(dest).strip().lower() not in ["not set", "none", ""]:
        dest_title = str(dest).strip().title()
        meta = dict(callback_context.state.get("__session_metadata__") or {})
        target_name = f"Trip to {dest_title}"
        if meta.get("displayName") != target_name:
            meta["displayName"] = target_name
            callback_context.state["__session_metadata__"] = meta
            logging.info(f"[Session Name] Updated chat title to: {target_name}")
        return

    # If destination is not explicitly set yet, check recent user query events
    events = getattr(callback_context.session, "events", [])
    for event in reversed(events):
        if getattr(event, "author", "") == "user":
            content = getattr(event, "content", None)
            text = ""
            if content and hasattr(content, "parts"):
                for part in content.parts:
                    if hasattr(part, "text") and part.text:
                        text += " " + part.text
            text = text.strip()
            if not text:
                continue

            match = re.search(
                r'(?:trip to|travel to|visit|vacation in|holiday in|flight to|flights to|fly to|hotel in|hotels in|go to|going to|tour of)\s+([A-Za-z\s,]+)',
                text,
                re.IGNORECASE
            )
            if match:
                raw_dest = match.group(1).strip()
                raw_dest = re.split(r'\b(for|in|from|with|during|next|this|on|please|can|could|and)\b', raw_dest, flags=re.IGNORECASE)[0].strip()
                raw_dest = raw_dest.strip(",.!? ")
                if raw_dest and len(raw_dest) > 1 and raw_dest.lower() not in ["a", "the", "somewhere", "anywhere"]:
                    dest_title = raw_dest.title()
                    callback_context.state["destination"] = dest_title
                    meta = dict(callback_context.state.get("__session_metadata__") or {})
                    meta["displayName"] = f"Trip to {dest_title}"
                    callback_context.state["__session_metadata__"] = meta
                    logging.info(f"[Session Name] Inferred chat title from user query: Trip to {dest_title}")
                    return


def _init_session_state(callback_context: CallbackContext) -> None:
    """Pre-populates session state keys so agent templates and tools operate seamlessly."""
    defaults = {
        "destination": "Not set",
        "travel_dates": "Not set",
        "attractions": [],
        "itinerary": "None yet",
        "selected_flight": "None",
        "selected_hotel": "None"
    }
    for key, value in defaults.items():
        if key not in callback_context.state:
            callback_context.state[key] = value
    _sync_destination_session_title(callback_context)


def _after_agent_sync_title(callback_context: CallbackContext) -> None:
    """Post-agent hook to ensure destination is synced to chat title."""
    _sync_destination_session_title(callback_context)


# ==========================================
# Core Stateful Planning & Export Tools
# ==========================================

def save_attractions_to_state(
    tool_context: ToolContext,
    attractions: List[str]
) -> dict:
    """Saves or appends a list of attractions to the user's travel wishlist in session state.

    Args:
        attractions: A list of attraction names or activities to save.

    Returns:
        dict: A status confirmation with the updated list of attractions.
    """
    existing_attractions = tool_context.state.get("attractions", [])
    for item in attractions:
        if item not in existing_attractions:
            existing_attractions.append(item)
    
    tool_context.state["attractions"] = existing_attractions
    logging.info(f"[State Update] Attractions saved: {existing_attractions}")
    return {
        "status": "success",
        "message": f"Added {len(attractions)} attraction(s) to wishlist.",
        "current_attractions": existing_attractions
    }


def remove_attraction_from_state(
    tool_context: ToolContext,
    attraction: str
) -> dict:
    """Removes a specific attraction from the user's saved wishlist in session state.

    Args:
        attraction: The name of the attraction to remove.

    Returns:
        dict: Status message and the remaining attractions list.
    """
    existing = tool_context.state.get("attractions", [])
    updated = [item for item in existing if attraction.lower() not in item.lower()]
    tool_context.state["attractions"] = updated
    logging.info(f"[State Update] Removed attraction: {attraction}")
    return {
        "status": "success",
        "message": f"Removed '{attraction}' if present.",
        "remaining_attractions": updated
    }


def save_itinerary_to_state(
    tool_context: ToolContext,
    destination: str,
    duration_days: int,
    itinerary_text: str
) -> dict:
    """Stores the generated day-by-day travel itinerary into the session state.

    Args:
        destination: Name of the destination city or country.
        duration_days: Duration of the trip in days.
        itinerary_text: The complete day-by-day schedule text or markdown.

    Returns:
        dict: Confirmation of the saved itinerary.
    """
    tool_context.state["destination"] = destination
    tool_context.state["duration_days"] = duration_days
    tool_context.state["itinerary"] = itinerary_text

    if destination and str(destination).strip().lower() not in ["not set", "none", ""]:
        dest_title = str(destination).strip().title()
        meta = dict(tool_context.state.get("__session_metadata__") or {})
        meta["displayName"] = f"Trip to {dest_title}"
        tool_context.state["__session_metadata__"] = meta
        logging.info(f"[Session Name] Updated chat title to: {meta['displayName']}")

    logging.info(f"[State Update] Itinerary saved for {destination} ({duration_days} days)")
    return {
        "status": "success",
        "destination": destination,
        "duration_days": duration_days,
        "message": "Itinerary successfully saved to session state."
    }


def export_trip_to_file(
    tool_context: ToolContext,
    filename: str,
    content: str
) -> dict:
    """Exports the full travel guide, itinerary, bookings, and tips into a Markdown file under saved_trips/.

    Args:
        filename: Name of the file to create (e.g., 'Japan_7Day_Trip_Plan.md').
        content: The complete markdown content of the travel guide.

    Returns:
        dict: Confirmation with saved file path.
    """
    if not filename.endswith(".md"):
        filename = f"{filename}.md"
    
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    output_dir = os.path.join(base_dir, "saved_trips")
    os.makedirs(output_dir, exist_ok=True)
    
    file_path = os.path.join(output_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    logging.info(f"[Export] Saved trip plan to {file_path}")
    return {
        "status": "success",
        "file_path": file_path,
        "message": f"Trip plan successfully exported to {filename} in saved_trips folder."
    }


def view_trip_summary(tool_context: ToolContext) -> dict:
    """Retrieves the current saved trip details, bookings, wishlist attractions, and itinerary from session state.

    Returns:
        dict: The current state including destination, flight/hotel bookings, attractions list, and itinerary status.
    """
    destination = tool_context.state.get("destination", "Not set")
    travel_dates = tool_context.state.get("travel_dates", "Not set")
    flight = tool_context.state.get("selected_flight", "None")
    hotel = tool_context.state.get("selected_hotel", "None")
    attractions = tool_context.state.get("attractions", [])
    has_itinerary = "itinerary" in tool_context.state and tool_context.state["itinerary"] != "None yet"
    
    return {
        "destination": destination,
        "travel_dates": travel_dates,
        "booked_flight": flight,
        "booked_hotel": hotel,
        "saved_attractions_count": len(attractions),
        "saved_attractions": attractions,
        "has_itinerary": has_itinerary
    }


# ==========================================
# Specialized Sub-Agents
# ==========================================

destination_finder = Agent(
    name="destination_finder",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Helps travelers brainstorm and select the perfect destination based on budget, season, and travel style.",
    instruction="""
    You are an expert travel consultant specializing in destination discovery.
    
    INSTRUCTIONS:
    - Help users identify their ideal destination by inquiring about:
      1. Travel style: adventure, relaxation, historical/cultural, culinary, beach, city nightlife.
      2. Companions: solo, couple, family with kids, group of friends.
      3. Budget tier: backpacker/budget, mid-range, luxury.
      4. Travel duration and time of year (seasonality/weather).
    - Provide 2-3 compelling destination suggestions with a brief explanation of why each fits their criteria and the best time to visit.
    - When the user selects a destination, save it using 'memorize' (key='destination', value=<chosen destination>) and suggest exploring attractions or booking flights/hotels.
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[memorize, recall, view_trip_summary],
)

attractions_guide = Agent(
    name="attractions_guide",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Curates must-see landmarks, cultural heritage, hidden local gems, and culinary experiences.",
    instruction="""
    You are a local insider and sightseeing specialist.
    
    INSTRUCTIONS:
    - Provide curated attractions and experiences for the user's destination, grouped logically (e.g. iconic landmarks, hidden gems, food experiences, nature walks).
    - When the user selects or expresses interest in specific attractions, use your 'save_attractions_to_state' tool to record them in their wishlist.
    - If the user wants to remove an attraction, use 'remove_attraction_from_state'.
    - If the user asks to review what they have saved, use 'view_trip_summary' or display their saved attractions.
    - Always suggest a good mix of famous sights and unique local activities.
    - When ready, offer to pass these to the itinerary architect or booking specialist.
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[save_attractions_to_state, remove_attraction_from_state, view_trip_summary, recall],
)

booking_specialist = Agent(
    name="booking_specialist",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Assists with searching flights, reserving seats, finding hotels, and selecting rooms.",
    instruction="""
    You are a professional travel booking concierge.
    
    INSTRUCTIONS:
    - When the user needs flight options:
      1. Use 'search_flights' with origin, destination, and departure date.
      2. Present top options with airline, timings, duration, and price.
      3. Always provide the clickable direct booking links (Google Flights, Skyscanner, Kayak) returned by the tool so the user can view live seat maps and book in real-time.
      4. When the user picks a flight, use 'select_seat' to assign their preferred seat.
      5. Save the confirmed flight with 'memorize' (key='selected_flight', value='<flight_id> Seat <seat>').
    - When the user needs accommodation:
      1. Use 'search_hotels' with destination and check-in/out dates.
      2. Present top star-rated options with prices, neighborhoods, and amenities.
      3. Always provide the clickable direct reservation links (Booking.com, Expedia, Google Hotels) returned by the tool so the user can inspect live rooms and reserve instantly.
      4. When the user picks a hotel, use 'select_room' to confirm their room category.
      5. Save the confirmed hotel with 'memorize' (key='selected_hotel', value='<hotel_name> - <room_type>').
    - Keep track of travel dates using 'memorize' (key='travel_dates', value='<dates>').
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[search_flights, select_seat, search_hotels, select_room, memorize, recall, view_trip_summary],
)

itinerary_architect = Agent(
    name="itinerary_architect",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Designs structured, optimized day-by-day travel itineraries with smart geographical grouping.",
    instruction="""
    You are a master itinerary designer.
    
    INSTRUCTIONS:
    - Take the user's selected destination, duration, saved attractions, and any confirmed flight/hotel details:
      Saved Attractions: { attractions? }
      Booked Flight: { selected_flight? }
      Booked Hotel: { selected_hotel? }
    - Structure each day realistically:
      - Morning: Sight 1 / Activity
      - Lunch: Recommended local dish or dining neighborhood
      - Afternoon: Sight 2 / Exploration
      - Evening: Dinner, sunset spot, or night entertainment
    - Group activities geographically to minimize transit time.
    - Use the 'save_itinerary_to_state' tool to save the itinerary once generated.
    - Offer to export the complete plan to a file using the 'export_trip_to_file' tool.
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[save_itinerary_to_state, export_trip_to_file, view_trip_summary, recall],
)

in_trip_concierge = Agent(
    name="in_trip_concierge",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Real-time in-trip assistant for flight delays, terminal information, local directions, and dining tips.",
    instruction="""
    You are an on-the-ground travel concierge assisting travelers who are actively on their journey.
    
    INSTRUCTIONS:
    - For flight inquiries or status updates:
      - Use 'check_flight_status' to fetch real-time gate, terminal, baggage carousel, and delay information.
    - For in-destination support:
      - Provide practical advice on getting around, navigating train stations, authentic dining near their location, and local etiquette.
      - Handle unexpected schedule disruptions with calm, actionable alternatives.
    - Keep responses rapid, clear, and reassuring.
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[check_flight_status, recall, view_trip_summary],
)

logistics_advisor = Agent(
    name="logistics_advisor",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Provides practical travel logistics, budget estimates, transit advice, packing checklists, and local etiquette.",
    instruction="""
    You are a practical travel logistics and budget advisor.
    
    INSTRUCTIONS:
    - Provide actionable, realistic travel advice for the destination:
      1. Transportation: Airport transfers, transit passes (e.g., JR Pass, Metro cards), ride-hailing tips.
      2. Budget Breakdown: Estimated daily costs for budget, mid-range, and high-end travel.
      3. Packing Essentials: Weather-appropriate clothing, adapters, power banks, essentials.
      4. Local Customs & Etiquette: Tipping culture, dress codes for temples/churches, payment preferences (cash vs. card).
      5. Safety & Health: Emergency numbers, travel insurance recommendations, tap water safety.
    - You can use the 'export_trip_to_file' tool if the user wants to save their logistics guide into a file.
    """,
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
    tools=[export_trip_to_file, view_trip_summary, recall],
)

# ==========================================
# Root Coordinator Agent
# ==========================================

travel_coordinator = Agent(
    name="travel_coordinator",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="Main Travel Concierge coordinating your entire travel planning, booking, and in-trip experience.",
    instruction="""
    You are the head Travel Concierge and Coordinator.
    
    Your mission is to understand where the traveler is in their planning journey and seamlessly guide them:
    
    1. If they don't know where to travel or need inspiration -> delegate to 'destination_finder'.
    2. If they have a destination and want to discover sights, food, or activities -> delegate to 'attractions_guide'.
    3. If they want to search flights, reserve seats, find hotels, or select rooms -> delegate to 'booking_specialist'.
    4. If they have chosen attractions and want a daily schedule or day-by-day plan -> delegate to 'itinerary_architect'.
    5. If they are actively traveling and need flight status, airport gates, or on-the-ground tips -> delegate to 'in_trip_concierge'.
    6. If they have questions on budget, transit passes, packing, visas, or customs -> delegate to 'logistics_advisor'.
    7. If they want an overview of their trip or want to download/export their complete itinerary, use 'view_trip_summary' or 'export_trip_to_file'.

    Always be welcoming, knowledgeable, organized, and inspiring!
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2,
    ),
    sub_agents=[
        destination_finder,
        attractions_guide,
        booking_specialist,
        itinerary_architect,
        in_trip_concierge,
        logistics_advisor
    ],
    tools=[view_trip_summary, export_trip_to_file, check_flight_status],
    before_agent_callback=_init_session_state,
    after_agent_callback=_after_agent_sync_title,
)

# ==========================================
# App & Plugins Setup
# ==========================================

graceful_plugin = Graceful429Plugin(
    name="graceful_429_plugin",
    fallback_text={
        "default": "**[Travel Concierge - Quota Fallback]** The model API is temporarily out of quota. Please retry in a few moments."
    }
)
graceful_plugin.apply_429_interceptor(travel_coordinator)

app = App(
    name="travel_planner",
    root_agent=travel_coordinator,
    plugins=[graceful_plugin]
)
