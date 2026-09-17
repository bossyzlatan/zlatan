import aiohttp

# Updated URL
BINLIST_URL = "https://bins.antipublic.cc/bins/{}"

async def get_bin_info(bin_number: str) -> dict:
    """
    Fetch BIN information from the antipublic.cc API.
    """
    if not bin_number.isdigit() or len(bin_number) < 6:
        return {"error": "Invalid BIN. Must be at least 6 digits."}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(BINLIST_URL.format(bin_number)) as resp:
                if resp.status == 429:
                    return {"error": "Rate limit exceeded. Try again later."}
                if resp.status == 404:
                    return {"error": "BIN not found."}
                if resp.status != 200:
                    return {"error": f"API request failed (status {resp.status})"}

                data = await resp.json()

                # Note: The new API does not return a 'success' field in the JSON, 
                # so we skip the success check and proceed to map the data.

                return {
                    "bin": data.get("bin"),
                    "length": "N/A",  # Not provided by new API
                    "luhn": "N/A",    # Not provided by new API
                    "scheme": data.get("brand"),  # 'brand' in new API (e.g., VISA) maps to 'scheme'
                    "type": data.get("type"),
                    "brand": data.get("level"),   # 'level' in new API (e.g., CLASSIC) maps to 'brand'/category
                    "bank": data.get("bank"),
                    "bank_phone": "N/A", # Not provided by new API
                    "bank_url": "N/A",   # Not provided by new API
                    "country": data.get("country_name"),
                    "country_emoji": data.get("country_flag"),
                }
        except Exception as e:
            return {"error": f"Exception: {str(e)}"}
