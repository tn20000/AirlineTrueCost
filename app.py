import os
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
import datetime
import json

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()

# Load API keys from .env file
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TRAVELPAYOUTS_MARKER = os.getenv("TRAVELPAYOUTS_MARKER")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")

# Constants
SOCAL_AIRPORTS = ["SNA", "LAX", "SAN", "ONT", "LGB", "BUR", "PSP", "CLD"]
SECURITY_BUFFERS = {"LAX": 2.0, "SNA": 1.0, "SAN": 1.0, "ONT": 1.0, "LGB": 1.0, "BUR": 1.0, "PSP": 1.0, "CLD": 1.0}
# Affiliate Links - Loaded from .env file
AIRHELP_AFFILIATE_LINK = os.getenv("AIRHELP_AFFILIATE_LINK")
AVIASALES_BASE_URL = os.getenv("AVIASALES_BASE_URL")
COMPENSAIR_AFFILIATE_LINK = os.getenv("COMPENSAIR_AFFILIATE_LINK")


# --- 2. API & CALCULATION FUNCTIONS ---

def get_ground_travel_data(origin_address, airport_codes):
    """Calls Google Maps Distance Matrix API."""
    if not origin_address:
        return {}
    
    destinations = "|".join([f"{code} Airport" for code in airport_codes])
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin_address,
        "destinations": destinations,
        "units": "imperial",
        "key": GOOGLE_MAPS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        results = {}
        if data.get("status") == "OK":
            elements = data["rows"][0]["elements"]
            for idx, code in enumerate(airport_codes):
                element = elements[idx]
                if element.get("status") == "OK":
                    miles = element["distance"]["value"] * 0.000621371
                    hours = element["duration"]["value"] / 3600.0
                    results[code] = {"miles": miles, "hours": hours}
                else:
                    results[code] = {"miles": 0, "hours": 0, "error": element.get("status")}
        else:
            st.error(f"Google Maps API Error: {data.get('error_message', data.get('status'))}")
        return results
    except requests.exceptions.RequestException as e:
        st.error(f"Error fetching ground data from Google Maps: {e}")
        return {}


def calculate_rideshare_cost(miles, hours):
    """Estimate Lyft/Uber Cost."""
    minutes = hours * 60
    return 5.00 + (miles * 1.20) + (minutes * 0.30)


def get_flight_data(origin_airport, dest_airport, departure_date, return_date):
    """
    Calls Travelpayouts Aviasales v3 API to find the cheapest flights.
    Returns a list of flight options sorted by price.
    """
    api_url = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
    
    params = {
        'origin': origin_airport,
        'destination': dest_airport,
        'currency': 'usd',
        'depart_date': departure_date.strftime('%Y-%m-%d'),
        'return_date': return_date.strftime('%Y-%m-%d') if return_date else '',
        'group_by': 'departure_at',
        'show_to_affiliates': 'true',
        'token': TRAVELPAYOUTS_TOKEN,
        'limit': 10
    }

    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        data = response.json()

        if not data.get("success") or not data.get("data"):
            return []

        flights = []
        flight_data_dict = data.get("data", {})
        
        for flight_info in flight_data_dict.values():
            price = flight_info.get('price')
            duration_min = flight_info.get('duration')
            
            if price is None or duration_min is None:
                continue

            duration_hr = duration_min / 60.0
            
            link_params = {
                "marker": TRAVELPAYOUTS_MARKER,
                "origin_iata": origin_airport,
                "destination_iata": dest_airport,
                "depart_date": departure_date.strftime('%Y-%m-%d'),
                "return_date": return_date.strftime('%Y-%m-%d') if return_date else '',
                "adults": 1,
                "children": 0,
                "infants": 0,
                "trip_class": 0, # Economy
            }
            deep_link = f"{AVIASALES_BASE_URL}/search?{requests.compat.urlencode(link_params)}"

            flights.append({
                "price": price,
                "duration": duration_hr,
                "airline": flight_info.get("airline"),
                "flight_number": flight_info.get("flight_number"),
                "link": deep_link,
                "transfers": flight_info.get("transfers", 0)
            })
        
        return sorted(flights, key=lambda x: x['price'])

    except requests.exceptions.RequestException as e:
        st.warning(f"Could not fetch flights for {origin_airport}: {e}")
        return []
    except json.JSONDecodeError:
        st.warning(f"Could not parse flight data for {origin_airport}. The API may be down.")
        return []


# --- 3. STREAMLIT UI ---

st.set_page_config(layout="wide", page_title="SoCal Airfare True Cost Calculator")

# --- Sidebar Inputs ---
with st.sidebar:
    st.header("Your Trip Details")
    home_address = st.text_input("Home or Starting Address", "23539 Spectrum, Irvine, CA")
    dest_airport = st.text_input("Destination Airport (e.g., JFK, LHR)", "CLT")
    
    today = datetime.date.today()
    departure_date = st.date_input("Departure Date", today + datetime.timedelta(days=30))
    return_date = st.date_input("Return Date (Optional)", None)
    
    st.header("Cost Factors")
    value_of_time = st.slider("Your Value of Time ($/hr)", 0, 500, 150)
    include_rideshare = st.toggle("Include Rideshare Cost", True)
    
    calculate_button = st.button("Calculate True Cost", type="primary")


# --- Main Page ---
st.title("✈️ Airfare True Cost Calculator")
st.markdown("Find the *actual* best airport to fly out of in Southern California.")

if calculate_button:
    # --- Input Validation ---
    if not home_address or not dest_airport:
        st.error("Please enter a valid Home Address and Destination Airport.")
    elif not GOOGLE_MAPS_API_KEY or not TRAVELPAYOUTS_TOKEN or TRAVELPAYOUTS_TOKEN == "YOUR_TOKEN_HERE":
        st.error("API keys are not configured. Please check your .env file.")
    else:
        with st.spinner("Calculating routes... This may take a moment."):
            # 1. Get Ground Travel Data for all airports
            ground_data = get_ground_travel_data(home_address, SOCAL_AIRPORTS)
            if not ground_data:
                st.stop()

            all_results = []

            # 2. Iterate through each SoCal airport
            for airport in SOCAL_AIRPORTS:
                g_stats = ground_data.get(airport)
                if not g_stats or g_stats['miles'] == 0:
                    st.write(f"⚠️ Could not calculate driving route to **{airport}**. Skipping.")
                    continue

                drive_miles = g_stats['miles']
                drive_hours = g_stats['hours']

                # 3. Get Flight Data for this airport
                flight_options = get_flight_data(airport, dest_airport, departure_date, return_date)
                if not flight_options:
                    continue # Silently skip if no flights, get_flight_data shows a warning

                # 4. Calculate True Cost for the best flight option from this airport
                best_flight = flight_options[0] # The API returns them sorted by price
                
                # Determine multipliers for one-way vs. round-trip
                trip_multiplier = 2 if return_date else 1
                
                total_ground_time = drive_hours * trip_multiplier
                total_buffer_time = SECURITY_BUFFERS[airport] * trip_multiplier
                rideshare_cost = calculate_rideshare_cost(drive_miles, drive_hours) * trip_multiplier if include_rideshare else 0.0

                total_time_hours = total_ground_time + total_buffer_time + best_flight["duration"]
                time_penalty = total_time_hours * value_of_time
                true_cost = best_flight["price"] + rideshare_cost + time_penalty

                all_results.append({
                    "Origin Airport": airport,
                    "Flight Price": best_flight["price"],
                    "Drive Time": f"{drive_hours:.1f} hrs",
                    "True Cost": true_cost,
                    "Rideshare Cost": rideshare_cost,
                    "Time Penalty": time_penalty,
                    "Total Time (hrs)": total_time_hours,
                    "Link": best_flight["link"],
                    "Transfers": best_flight["transfers"],
                    "_sort_value": true_cost
                })

            # --- 5. Display Results ---
            if not all_results:
                st.error(f"No flights found from any SoCal airport to **{dest_airport}** for the selected dates. Try adjusting the dates or destination.")
                st.stop()
            
            results_df = pd.DataFrame(all_results).sort_values(by="_sort_value").reset_index(drop=True)
            top_3 = results_df.head(3)

            st.header("🏆 Top 3 Optimal Routes")
            
            cols = st.columns(3)
            for i, row in top_3.iterrows():
                with cols[i]:
                    st.metric(
                        label=f"#{i+1} Option: Fly from {row['Origin Airport']}",
                        value=f"${row['True Cost']:.2f}",
                        delta=f"Flight: ${row['Flight Price']:.2f}",
                        delta_color="off"
                    )
                    st.caption(f"🚗 Drive: {row['Drive Time']} | 💰 Rideshare: ${row['Rideshare Cost']:.2f}")

                    st.link_button(f"Book on Aviasales", row['Link'], use_container_width=True)
                    


            with st.expander("View Detailed Cost Breakdown"):
                st.dataframe(results_df[[
                    "Origin Airport", "True Cost", "Flight Price", "Rideshare Cost", "Time Penalty", "Total Time (hrs)", "Drive Time"
                ]].style.format({
                    "True Cost": "${:,.2f}", "Flight Price": "${:,.2f}", "Rideshare Cost": "${:,.2f}", 
                    "Time Penalty": "${:,.2f}", "Total Time (hrs)": "{:.2f}"
                }))

        st.header("Enhance Your Trip")
        col1, col2 = st.columns(2)
        with col1:
            st.info("✈️ Flight delayed or canceled?")
            st.link_button("Claim Your Compensation with Compensair", COMPENSAIR_AFFILIATE_LINK, use_container_width=True)
        with col2:
            st.info("✈️ Flying out of LAX or another major hub?")
            st.link_button("Claim up to $600 for flight delays with AirHelp", AIRHELP_AFFILIATE_LINK, use_container_width=True)
