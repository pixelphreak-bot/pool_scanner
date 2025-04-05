import os
import sqlite3
import requests
from web3 import Web3
from dotenv import load_dotenv
import json
import time
import pandas as pd

# Load environment variables
load_dotenv()

BSC_RPC_URL = os.getenv("BSC_RPC_URL")
BSCSCAN_API_KEYS = [
    os.getenv("BSCSCAN_API_KEY"),
    os.getenv("BSCSCAN_API_KEY_2"),
    os.getenv("BSCSCAN_API_KEY_3")
]

# Initialize web3
web3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))

# === Dexscreener config ===
DEXSCREENER_BASE = "https://api.dexscreener.com/token-pairs/v1"
CHAIN_ID = "bsc"
LIQUIDITY_THRESHOLD = 1_000_000
REQUEST_SLEEP = 0.25
TOKEN_ADDRESSES = {
    "WBNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "USDT": "0x55d398326f99059fF775485246999027B3197955"
}

# === Default fees ===
DEFAULT_FEES = {
    "uniswapv2": 0.003,
    "pancakeswapv2": 0.0025,
    "biswap": 0.002,
    "apeswapv2": 0.002,
    "thenav2": 0.0001,
    "swychv2": 0.0025
}

# === Database setup ===
def init_db(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS token_pairs (
            exchange TEXT,
            pair_address TEXT PRIMARY KEY,
            token0 TEXT,
            token1 TEXT,
            symbol0 TEXT,
            symbol1 TEXT,
            pair_symbol TEXT,
            fee REAL,
            decimals0 INTEGER,
            decimals1 INTEGER,
            abi_data TEXT
        )
        '''
    )
    conn.commit()
    return conn

# === Fetch pools ===
def fetch_token_pools(token_address):
    url = f"{DEXSCREENER_BASE}/{CHAIN_ID}/{token_address}"
    try:
        response = requests.get(url)
        if response.status_code != 200:
            print(f"❌ Failed to fetch pools for {token_address}: {response.status_code}")
            return []
        return response.json()
    except requests.RequestException as e:
        print(f"❌ Error fetching pools: {e}")
        return []

def extract_info(pool):
    try:
        return {
            "dex": pool.get("dexId"),
            "pair_address": pool.get("pairAddress"),
            "liquidity_usd": float(pool.get("liquidity", {}).get("usd", 0)),
            "volume_usd": float(pool.get("volume", {}).get("h24", 0))
        }
    except (KeyError, TypeError, ValueError):
        return None

def collect_filtered_pairs():
    results = []
    for symbol, address in TOKEN_ADDRESSES.items():
        print(f"🔍 Fetching pools for {symbol}...")
        pools = fetch_token_pools(address)
        time.sleep(REQUEST_SLEEP)
        for pool in pools:
            info = extract_info(pool)
            if info and info["liquidity_usd"] >= LIQUIDITY_THRESHOLD:
                results.append(info)
    return results

# === BSCScan ABI + Web3 utilities ===
def get_contract_abi(pair_address):
    for key in BSCSCAN_API_KEYS:
        try:
            params = {
                "module": "contract",
                "action": "getabi",
                "address": pair_address,
                "apikey": key
            }
            response = requests.get("https://api.bscscan.com/api", params=params)
            data = response.json()
            if data["status"] == "1":
                return data["result"]
        except requests.RequestException:
            continue
    raise Exception("Failed to fetch ABI from BSCScan")

def get_token_addresses(pair_contract):
    return pair_contract.functions.token0().call(), pair_contract.functions.token1().call()

def get_token_decimals(token_address):
    abi = '[{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]'
    contract = web3.eth.contract(address=token_address, abi=abi)
    return contract.functions.decimals().call()

def get_token_symbol(token_address):
    abi = '[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"}]'
    contract = web3.eth.contract(address=token_address, abi=abi)
    try:
        return contract.functions.symbol().call()
    except (ValueError, Exception):
        return "UNKNOWN"

def get_dynamic_fee(pair_contract):
    for method in ["swapFee", "getSwapFee", "fee"]:
        try:
            func = getattr(pair_contract.functions, method)
            return func().call() / 1e6
        except (AttributeError, ValueError):
            continue
    return None

def identify_dex_version(abi):
    try:
        if any(method.get("name") == "slot0" for method in abi):
            return "v3"
    except (TypeError, AttributeError):
        pass
    return "v2"

def insert_token_pair(conn, data):
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT OR REPLACE INTO token_pairs (
            exchange, pair_address, token0, token1,
            symbol0, symbol1, pair_symbol, fee,
            decimals0, decimals1, abi_data
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', data)
    conn.commit()

# === Main logic ===
def main():
    print("\n🌐🌐 PixelPhreak Pool Scanner 🌐🌐\n")
    choice = input("Do you want to export the raw liquidity data to an Excel file before scanning? (Y/N): ").strip().lower()

    db_path = "token_pairs.db"
    conn = init_db(db_path)
    pairs = collect_filtered_pairs()

    excel_data = []
    inserted_count = 0

    for pair in pairs:
        dex_base = pair["dex"].strip().lower()
        pair_address = pair["pair_address"].strip()

        print(f"\nProcessing {pair_address} from DEX: {dex_base}")
        try:
            abi_json = get_contract_abi(pair_address)
            abi = json.loads(abi_json)
            pair_contract = web3.eth.contract(address=pair_address, abi=abi)

            token0, token1 = get_token_addresses(pair_contract)
            symbol0 = get_token_symbol(token0)
            symbol1 = get_token_symbol(token1)
            decimals0 = get_token_decimals(token0)
            decimals1 = get_token_decimals(token1)
            pair_symbol = f"{symbol0}/{symbol1}"

            dex_version = identify_dex_version(abi)
            exchange_full = f"{dex_base}{dex_version}"

            if dex_version == "v3":
                fee = get_dynamic_fee(pair_contract)
                print(f"Identified as V3, dynamic fee: {fee}")
            else:
                fee = DEFAULT_FEES.get(exchange_full)
                if fee is None:
                    fee = 0.003
                    print(f"⚠️⚠️ SWAP FEES NOT FOUND DEFAULTED TO {fee:.4f} ⚠️⚠️")
                else:
                    print(f"Identified as V2, default fee: {fee}")

            insert_token_pair(
                conn,
                (
                    exchange_full,
                    pair_address,
                    token0,
                    token1,
                    symbol0,
                    symbol1,
                    pair_symbol,
                    fee,
                    decimals0,
                    decimals1,
                    abi_json
                )
            )

            excel_data.append({
                "dex": exchange_full,
                "pair_symbol": pair_symbol,
                "pair_address": pair_address,
                "volume_usd": pair["volume_usd"],
                "liquidity_usd": pair["liquidity_usd"]
            })

            inserted_count += 1
            print("✅ Entry added.")
        except Exception as e:
            print(f"❌ Failed to process {pair_address}: {e}")

    if choice == 'y':
        df = pd.DataFrame(excel_data)
        df = df.sort_values(by="liquidity_usd", ascending=False)[[
            "dex", "pair_symbol", "pair_address", "volume_usd", "liquidity_usd"
        ]]
        df.to_excel("liquidity_data.xlsx", index=False)
        print("✅ Excel export completed: liquidity_data.xlsx\n")

    print(f"\n💰 {inserted_count} pairs met your liquidity threshold and have been added to the database. 💰")

if __name__ == "__main__":
    main()
