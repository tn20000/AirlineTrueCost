import os
import requests
import pandas as pd
from dotenv import load_dotenv

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()  # Load API keys from .env file

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

# User Inputs
USER_HOME_ADDRESS = "23539 Spectrum, Irvine, CA"  # Example user address
DESTINATION_AIRPORT = "CLT"  # Destination Airport Code
TRAVEL_DATE = "2026-03-31"   # Date for flight search (YYYY-MM-DD)
RETURN_DATE = "2026-04-05"   # Return date (YYYY-MM-DD). Set to None for one-way.
USER_HOURLY_RATE = 153.06     # Value of time ($/hr)
INCLUDE_RIDESHARE = False    # Set to True to include Lyft/Uber cost
EXCLUDED_AIRLINES = ["F9", "NK"] # Airlines to exclude (IATA Codes: Frontier, Spirit)

# Airports to analyze
SOCAL_AIRPORTS = ["SNA", "LAX", "SAN", "ONT", "LGB", "BUR", "PSP", "CLD"]


def get_ground_travel_data(origin_address, airport_codes):
    """
    Step 1: Calls Google Maps Distance Matrix API.
    Returns a dict: { 'SNA': {'miles': 10.5, 'hours': 0.3}, ... }
    """
    # Prepare destinations string (pipe-separated)
    # Appending "Airport" helps Google Maps identify the location correctly
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
        data = response.json()
        
        results = {}
        if data.get("status") == "OK":
            elements = data["rows"][0]["elements"]
            for idx, code in enumerate(airport_codes):
                element = elements[idx]
                if element.get("status") == "OK":
                    # Convert meters to miles (1 meter = 0.000621371 miles)
                    miles = element["distance"]["value"] * 0.000621371
                    # Convert seconds to hours
                    hours = element["duration"]["value"] / 3600.0
                    results[code] = {"miles": miles, "hours": hours}
                else:
                    results[code] = {"miles": 0, "hours": 0}
        return results
    except Exception as e:
        print(f"Error fetching ground data: {e}")
        return {}


def calculate_rideshare_cost(miles, hours):
    """
    Step 2: Estimate Lyft/Uber Cost.
    Formula: Base ($5) + (Miles * $1.20) + (Minutes * $0.30)
    """
    minutes = hours * 60
    return 5.00 + (miles * 1.20) + (minutes * 0.30)


def get_flight_data(origin, destination, date, return_date=None, hourly_rate=0, limit=3):
    """
    Step 3: Calls SerpApi (Google Flights).
    Iterates through all flights to find the one with the lowest 'True Cost'
    (Price + Duration * Hourly Rate), rather than just the cheapest.
    Handles both One-Way and Round-Trip searches natively.
    Returns a list of the top 'limit' flight options.
    """
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "currency": "USD",
        "hl": "en",
        "api_key": SERPAPI_API_KEY,
        "exclude_airlines": ",".join(EXCLUDED_AIRLINES)
    }
    
    if return_date:
        params["return_date"] = return_date
    else:
        params["type"] = "2"  # 2 = One-way
    
    try:
        response = requests.get("https://serpapi.com/search", params=params)
        data = response.json()
        
        # Combine 'best_flights' and 'other_flights' to widen the search
        all_flights = data.get("best_flights", []) + data.get("other_flights", [])
            
        if not all_flights:
            return []
            
        # 1. Pre-process and Score all candidates
        preliminary_list = []
        for flight in all_flights:
            price = flight.get("price")
            if price is None:
                continue
            
            segments = flight.get("flights", [])
            duration_minutes = flight.get("total_duration")
            if not duration_minutes:
                duration_minutes = sum(leg.get("duration", 0) for leg in segments)
            duration_hours = duration_minutes / 60.0
            
            # Heuristic score (Price + Duration * Rate)
            # For incomplete flights, this uses partial duration, effectively prioritizing them for deep fetch checks
            score = price + (duration_hours * hourly_rate)
            
            # Check if flight is incomplete (Outbound only)
            # If we want a return trip, but the last segment arrives at the destination (not back home), it's incomplete.
            is_incomplete = False
            if return_date and flight.get("departure_token"):
                last_dest = segments[-1].get("arrival_airport", {}).get("id")
                if last_dest != origin:
                    is_incomplete = True
            
            preliminary_list.append({
                "flight": flight,
                "score": score,
                "price": price,
                "duration": duration_hours,
                "is_incomplete": is_incomplete
            })

        # 2. Sort by Heuristic Score (Greedy Filter)
        preliminary_list.sort(key=lambda x: x["score"])
        
        # 3. Process candidates (limit deep fetches)
        candidates = []
        deep_fetch_count = 0
        max_deep_fetches = 10  # Allow more API calls than the output limit to handle failures
        
        for item in preliminary_list:
            if len(candidates) >= limit:
                break
                
            # Initialize legs containers
            out_legs = []
            ret_legs = []
            flight = item["flight"]
            
            if item["is_incomplete"]:
                if deep_fetch_count >= max_deep_fetches:
                    continue 
                    
                token = flight.get("departure_token")
                print(f"Fetching return leg for {origin} -> {destination}...")
                deep_fetch_count += 1
                
                params_2 = params.copy()
                params_2["departure_token"] = token
                # Remove parameters that are already encoded in the token to prevent conflicts
                # Also remove exclude_airlines as the token implies the filter from the first request
                for k in ["type", "exclude_airlines"]:
                    params_2.pop(k, None)

                try:
                    resp_2 = requests.get("https://serpapi.com/search", params=params_2)
                    data_2 = resp_2.json()
                    
                    if "error" in data_2:
                        print(f"  > API Error: {data_2['error']}")
                        continue

                    # Check both 'best_flights' and 'other_flights' for the return leg
                    best_return_opts = data_2.get("best_flights", []) + data_2.get("other_flights", [])
                    if best_return_opts:
                        # Find the best return option based on True Cost (Price + Duration * Rate)
                        best_ret_flight = None
                        best_ret_score = float('inf')

                        for ret_opt in best_return_opts:
                            r_price = ret_opt.get("price")
                            if r_price is None: continue
                            
                            r_dur_mins = ret_opt.get("total_duration")
                            if not r_dur_mins:
                                r_legs = ret_opt.get("flights", [])
                                r_dur_mins = sum(l.get("duration", 0) for l in r_legs)
                            r_score = r_price + ((r_dur_mins / 60.0) * hourly_rate)
                            
                            if r_score < best_ret_score:
                                best_ret_score = r_score
                                best_ret_flight = ret_opt
                        
                        if not best_ret_flight:
                            print(f"  > No return options with valid price found.")
                            continue

                        # MERGE LOGIC: Combine original outbound with new return
                        ret_flight = best_ret_flight
                        
                        out_legs = flight.get("flights", [])
                        ret_legs = ret_flight.get("flights", [])
                        
                        # Update price/duration from the return object (which usually holds the total bundle info)
                        item["price"] = ret_flight.get("price", item["price"])
                        
                        # ALWAYS sum the legs to get the true total duration.
                        # The `total_duration` on the `ret_flight` object can be misleading.
                        d_out = sum(leg.get("duration", 0) for leg in out_legs)
                        d_ret = sum(leg.get("duration", 0) for leg in ret_legs)
                        duration_minutes = d_out + d_ret
                            
                        item["duration"] = duration_minutes / 60.0
                        item["score"] = item["price"] + (item["duration"] * hourly_rate)
                    else:
                        print(f"  > No return options found for token.")
                        continue
                except Exception as e:
                    print(f"Error fetching return leg: {e}")
                    continue
            else:
                # Standard processing for already-bundled flights
                segments = flight.get("flights", [])

                # Recalculate duration by summing all legs to be safe
                duration_minutes = sum(leg.get("duration", 0) for leg in segments)
                item["duration"] = duration_minutes / 60.0
                item["score"] = item["price"] + (item["duration"] * hourly_rate)

                # Split segments into Outbound and Return based on destination airport
                # Strategy: Find the segment that ARRIVES at the destination. Everything after is return.
                split_idx = -1
                for i, seg in enumerate(segments):
                    if seg.get("arrival_airport", {}).get("id") == destination:
                        split_idx = i + 1
                        break
                    # Fallback: Check if next segment departs from destination
                    if seg.get("departure_airport", {}).get("id") == destination:
                        split_idx = i
                        break
                if split_idx != -1:
                    out_legs = segments[:split_idx]
                    ret_legs = segments[split_idx:]
                else:
                    out_legs = segments

            def fmt_leg(legs):
                if not legs: return "", ""
                nums = [f"{s.get('airline','')} {s.get('flight_number','')}" for s in legs]
                d = legs[0].get("departure_airport", {}).get("time", "").split(" ")[-1]
                a = legs[-1].get("arrival_airport", {}).get("time", "").split(" ")[-1]
                return "->".join(nums), f"{d}-{a}"

            out_desc, out_time = fmt_leg(out_legs)
            ret_desc, ret_time = fmt_leg(ret_legs)
            
            final_desc = f"OUT: {out_desc} | RET: {ret_desc}" if ret_desc else out_desc
            final_time = f"OUT: {out_time} | RET: {ret_time}" if ret_time else out_time
            
            candidates.append({
                "price": item["price"],
                "duration": item["duration"],
                "score": item["score"],
                "description": final_desc,
                "times": final_time
            })
            
        candidates.sort(key=lambda x: x["score"])
        return candidates
        
    except Exception as e:
        print(f"Error fetching flight data for {origin} to {destination}: {e}")
        return []


def calculate_true_cost():
    """
    Step 4: The 'True Cost' Engine.
    Combines Ground Data, Flight Data, and Time Penalty.
    """
    print(f"Calculating True Cost for trip to {DESTINATION_AIRPORT}...")
    if RETURN_DATE:
        print(f"Trip Type: Round Trip ({TRAVEL_DATE} to {RETURN_DATE})")
    else:
        print(f"Trip Type: One Way ({TRAVEL_DATE})")
    print(f"User Address: {USER_HOME_ADDRESS}")
    print("-" * 60)

    # 1. Get Ground Travel Data
    ground_data = get_ground_travel_data(USER_HOME_ADDRESS, SOCAL_AIRPORTS)
    
    results = []
    
    for airport in SOCAL_AIRPORTS:
        # Ground Data
        g_stats = ground_data.get(airport)
        if not g_stats or g_stats['miles'] == 0:
            print(f"Skipping {airport}: Could not calculate driving route.")
            continue
            
        drive_miles = g_stats['miles']
        drive_hours = g_stats['hours']
        
        # 3. Get Flight Data
        # Perform search (One-Way or Round-Trip based on RETURN_DATE)
        flight_options = get_flight_data(airport, DESTINATION_AIRPORT, TRAVEL_DATE, RETURN_DATE, USER_HOURLY_RATE)
        
        if not flight_options:
            print(f"Skipping {airport}: No flights found.")
            continue

        # 4. Calculate Time Penalty
        # Security Buffer: LAX = 2.0 hrs, others = 1.0 hr
        security_buffer = 2.0 if airport == "LAX" else 1.0
        
        if RETURN_DATE:
            # Round Trip: Double driving and buffer
            # Flight hours from API already includes both legs for round trips
            total_ground_time = (drive_hours * 2)
            total_buffer = (security_buffer * 2)
            lyft_multiplier = 2
        else:
            # One Way
            total_ground_time = drive_hours
            total_buffer = security_buffer
            lyft_multiplier = 1

        # 2. Estimate Ground Cost (Adjusted for round trip)
        if INCLUDE_RIDESHARE:
            lyft_cost = calculate_rideshare_cost(drive_miles, drive_hours) * lyft_multiplier
        else:
            lyft_cost = 0.0

        # Process top 3 combinations for this airport
        # Calculate True Cost for each combination
        airport_results = []
        for option in flight_options:
            total_time = total_ground_time + total_buffer + option["duration"]
            time_penalty = total_time * USER_HOURLY_RATE
            true_cost = option["price"] + lyft_cost + time_penalty
            
            airport_results.append({
                "Airport": airport,
                "Flight Details": option["description"],
                "Schedule": option["times"],
                "Ticket Price": f"${option['price']:.2f}",
                "Est. Lyft Cost": f"${lyft_cost:.2f}",
                "Total Time": round(total_time, 2),
                "True Cost": f"${true_cost:.2f}",
                "_sort_value": true_cost
            })

        # Sort by True Cost and take top 3 for this airport
        airport_results.sort(key=lambda x: x["_sort_value"])
        results.extend(airport_results[:3])

    # Step 5: Output
    if not results:
        return pd.DataFrame()
        
    df = pd.DataFrame(results)
    df = df.sort_values(by="_sort_value")
    df = df.drop(columns=["_sort_value"])
    if not INCLUDE_RIDESHARE:
        df = df.drop(columns=["Est. Lyft Cost"])
    return df

if __name__ == "__main__":
    if not GOOGLE_MAPS_API_KEY or not SERPAPI_API_KEY:
        print("ERROR: API keys not found. Please set GOOGLE_MAPS_API_KEY and SERPAPI_API_KEY in your .env file.")
    else:
        final_df = calculate_true_cost()
        if not final_df.empty:
            print("\n" + final_df.to_string(index=False))
        else:
            print("\nNo valid routes found.")