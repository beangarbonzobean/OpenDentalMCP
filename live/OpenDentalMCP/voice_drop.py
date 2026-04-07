#!/usr/bin/env python3
"""
Post-Op Voice Drop System
Queries Open Dental for completed major procedures, generates personalized
post-op care scripts using AI, converts to audio via ElevenLabs voice clone,
and delivers as ringless voicemail via Drop Cowboy.
"""

import json
import logging
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("voice_drop")

# ─── Procedure codes that trigger voice drops ───
DEFAULT_PROCEDURE_CODES = {
    "extractions": ["D7140", "D7210", "D7220", "D7230", "D7240", "D7241"],
    "implants": ["D6010", "D6012", "D6040", "D6050"],
    "root_canals": ["D3310", "D3320", "D3330"],
    "surgeries": ["D7951", "D7952", "D7953"],
}


def load_config(config_path: str = None) -> dict:
    """Load configuration from voice_drop_config.json."""
    if config_path is None:
        config_path = Path(__file__).parent / "voice_drop_config.json"
    with open(config_path) as f:
        config = json.load(f)

    required_keys = ["mcp_endpoint", "mcp_bearer_token", "anthropic_api_key"]
    for key in required_keys:
        if not config.get(key):
            raise ValueError(f"Missing required config key: {key}")

    # Set defaults
    config.setdefault("test_mode", True)
    config.setdefault("test_patient_name", "Ben Young")
    config.setdefault("procedure_codes", DEFAULT_PROCEDURE_CODES)
    config.setdefault("lookback_days", 1)
    config.setdefault("audio_output_dir", "voice_drops")
    config.setdefault("dry_run", False)
    config.setdefault("elevenlabs_model_id", "eleven_multilingual_v2")

    # Set up file logging
    log_file = config.get("log_file", "voice_drop.log")
    file_handler = logging.FileHandler(
        Path(__file__).parent / log_file, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)

    return config


# ─── MCP Integration ───

def call_mcp_tool(config: dict, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via the HTTP endpoint using JSON-RPC."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {
        "Authorization": f"Bearer {config['mcp_bearer_token']}",
        "Content-Type": "application/json",
    }
    resp = requests.post(config["mcp_endpoint"], json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"MCP error: {data['error']}")
    result = data.get("result", {})
    # JSON-RPC tools/call wraps result in content array
    if isinstance(result, dict) and "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                return json.loads(item["text"])
    return result


# ─── Step 1: Data Extraction ───

def get_completed_major_procedures(config: dict, target_date: str) -> list:
    """Query Open Dental for completed major procedures on target_date."""
    proc_codes = config.get("procedure_codes", DEFAULT_PROCEDURE_CODES)
    all_codes = []
    for codes in proc_codes.values():
        all_codes.extend(codes)
    codes_str = ", ".join(f"'{c}'" for c in all_codes)

    sql = f"""
    SELECT pl.ProcNum, pl.PatNum, pl.ProcDate, pl.ProcStatus,
           pc.ProcCode, pc.Descript AS ProcDescription, pl.ProvNum,
           p.FName, p.LName, p.WirelessPhone, p.HmPhone
    FROM procedurelog pl
    JOIN procedurecode pc ON pl.CodeNum = pc.CodeNum
    JOIN patient p ON pl.PatNum = p.PatNum
    WHERE pl.ProcDate = '{target_date}'
      AND pl.ProcStatus = 2
      AND pc.ProcCode IN ({codes_str})
    ORDER BY pl.PatNum, pl.ProcDate
    """

    logger.info(f"Querying procedures for date: {target_date}")
    result = call_mcp_tool(config, "query_database", {"query": sql})

    rows = result.get("results", result.get("rows", []))
    if not rows:
        logger.info("No completed major procedures found")
        return []

    logger.info(f"Found {len(rows)} completed major procedure(s)")
    return rows


# ─── Step 2: Filtering ───

def filter_patients(config: dict, procedures: list) -> list:
    """Filter and group procedures by patient."""
    if not procedures:
        return []

    # Group by PatNum
    patient_map = {}
    for proc in procedures:
        pat_num = str(proc.get("PatNum"))
        if pat_num not in patient_map:
            phone = proc.get("WirelessPhone", "") or proc.get("HmPhone", "")
            patient_map[pat_num] = {
                "PatNum": pat_num,
                "FName": proc.get("FName", ""),
                "LName": proc.get("LName", ""),
                "WirelessPhone": phone,
                "procedures": [],
            }
        patient_map[pat_num]["procedures"].append({
            "ProcCode": proc.get("ProcCode", ""),
            "ProcDescription": proc.get("ProcDescription", ""),
            "ProcNum": proc.get("ProcNum"),
        })

    patients = list(patient_map.values())

    # Test mode filter
    if config.get("test_mode"):
        test_name = config.get("test_patient_name", "Ben Young")
        parts = test_name.strip().split()
        first = parts[0].lower() if parts else ""
        last = parts[-1].lower() if len(parts) > 1 else ""
        patients = [
            p for p in patients
            if p["FName"].lower() == first and p["LName"].lower() == last
        ]
        logger.info(f"Test mode: filtered to {len(patients)} patient(s) matching '{test_name}'")

    # Skip patients without phone
    skipped = [p for p in patients if not p["WirelessPhone"].strip()]
    if skipped:
        for p in skipped:
            logger.warning(f"Skipping {p['FName']} {p['LName']} (PatNum {p['PatNum']}): no phone number")
    patients = [p for p in patients if p["WirelessPhone"].strip()]

    return patients


# ─── Step 3: Script Generation ───

def generate_voice_script(config: dict, patient: dict) -> str:
    """Generate a personalized ~30-second post-op care script using Claude API."""
    import anthropic

    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    proc_list = ", ".join(p["ProcDescription"] for p in patient["procedures"])

    # Determine procedure category for care instructions
    proc_codes = [p["ProcCode"] for p in patient["procedures"]]
    categories = config.get("procedure_codes", DEFAULT_PROCEDURE_CODES)
    proc_types = []
    for category, codes in categories.items():
        if any(c in codes for c in proc_codes):
            proc_types.append(category)

    system_prompt = """You are Dr. Ben Young, a dentist at Huntington Beach Dental Center.
You are recording a personalized post-op check-in voicemail for a patient.
Keep the message warm, caring, and concise (about 30 seconds when spoken, roughly 75-90 words).
Include:
1. A greeting using the patient's first name
2. A brief mention that you're checking in after their procedure
3. One or two specific care instructions relevant to their procedure type
4. An invitation to call the office if they have any concerns
5. A warm sign-off

Do NOT include any stage directions, brackets, or formatting — output ONLY the spoken words.
Sound natural, not robotic. Use contractions and casual phrasing."""

    user_prompt = f"""Generate a post-op voicemail script for:
- Patient: {patient['FName']}
- Procedure(s): {proc_list}
- Procedure type(s): {', '.join(proc_types)}
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    script = message.content[0].text.strip()
    logger.info(f"Generated script for {patient['FName']} {patient['LName']} ({len(script)} chars)")
    return script


# ─── Step 4: Audio Generation ───

def generate_audio(config: dict, script: str, patient: dict) -> str:
    """Convert script to audio using ElevenLabs API. Returns the file path."""
    if not config.get("elevenlabs_api_key"):
        logger.warning("ElevenLabs API key not configured — skipping audio generation")
        return ""

    from elevenlabs import ElevenLabs

    client = ElevenLabs(api_key=config["elevenlabs_api_key"])

    audio_dir = Path(__file__).parent / config.get("audio_output_dir", "voice_drops")
    audio_dir.mkdir(exist_ok=True)

    filename = f"{patient['PatNum']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
    filepath = audio_dir / filename

    audio_generator = client.text_to_speech.convert(
        text=script,
        voice_id=config["elevenlabs_voice_id"],
        model_id=config.get("elevenlabs_model_id", "eleven_multilingual_v2"),
        output_format="mp3_44100_128",
    )

    # ElevenLabs returns a generator — write chunks to file
    with open(filepath, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    file_size = filepath.stat().st_size
    logger.info(f"Audio generated: {filepath} ({file_size:,} bytes)")
    return str(filepath)


# ─── Step 5: Voicemail Delivery ───

def send_ringless_voicemail(config: dict, phone: str, audio_path: str) -> dict:
    """Send audio via Drop Cowboy API for ringless voicemail delivery."""
    if not config.get("drop_cowboy_api_key"):
        logger.warning("Drop Cowboy API key not configured — skipping delivery")
        return {"status": "skipped", "reason": "no_api_key"}

    api_url = "https://app.dropcowboy.com/api/rvm/send"
    headers = {
        "Authorization": f"Bearer {config['drop_cowboy_api_key']}",
    }

    # Clean phone number — digits only, ensure 10 or 11 digits
    clean_phone = "".join(c for c in phone if c.isdigit())
    if len(clean_phone) == 10:
        clean_phone = "1" + clean_phone

    with open(audio_path, "rb") as audio_file:
        files = {"audio_file": (os.path.basename(audio_path), audio_file, "audio/mpeg")}
        data = {
            "phone_number": clean_phone,
        }
        resp = requests.post(api_url, headers=headers, data=data, files=files, timeout=60)

    resp.raise_for_status()
    result = resp.json()
    logger.info(f"Voicemail sent to {clean_phone}: {result}")
    return result


# ─── Step 6: Logging to Open Dental ───

def create_commlog_entry(config: dict, patient: dict, script: str,
                         delivery_result: dict) -> dict:
    """Create a commlog entry in Open Dental recording the voice drop."""
    proc_descriptions = ", ".join(p["ProcDescription"] for p in patient["procedures"])
    delivery_status = delivery_result.get("status", "unknown")

    note = (
        f"[POST-OP VOICE DROP]\n"
        f"Procedures: {proc_descriptions}\n"
        f"Phone: {patient['WirelessPhone']}\n"
        f"Delivery status: {delivery_status}\n"
        f"---\n"
        f"Script:\n{script}"
    )

    result = call_mcp_tool(config, "create_commlog", {
        "PatNum": patient["PatNum"],
        "Note": note,
        "Mode_": "Phone",
        "SentOrReceived": "Sent",
    })

    if result.get("success"):
        logger.info(f"Commlog created for patient {patient['PatNum']}")
    else:
        logger.error(f"Failed to create commlog: {result.get('error')}")
    return result


# ─── Main Pipeline ───

def process_voice_drops(config: dict = None):
    """Main orchestrator for the voice drop pipeline."""
    logger.info("=" * 60)
    logger.info("POST-OP VOICE DROP PIPELINE STARTING")
    logger.info("=" * 60)

    if config is None:
        config = load_config()

    # Calculate target date
    lookback = config.get("lookback_days", 1)
    target_date = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
    logger.info(f"Target date: {target_date}")
    logger.info(f"Test mode: {config.get('test_mode', True)}")
    logger.info(f"Dry run: {config.get('dry_run', False)}")

    # Step 1: Get procedures
    procedures = get_completed_major_procedures(config, target_date)
    if not procedures:
        logger.info("No procedures found — nothing to do")
        return {"patients_processed": 0, "voice_drops_sent": 0, "errors": []}

    # Step 2: Filter patients
    patients = filter_patients(config, procedures)
    if not patients:
        logger.info("No eligible patients after filtering — nothing to do")
        return {"patients_processed": 0, "voice_drops_sent": 0, "errors": []}

    logger.info(f"Processing {len(patients)} patient(s)")

    # Step 3-6: Process each patient
    results = {"patients_processed": 0, "voice_drops_sent": 0, "errors": []}

    for patient in patients:
        pat_name = f"{patient['FName']} {patient['LName']}"
        logger.info(f"\n--- Processing: {pat_name} (PatNum: {patient['PatNum']}) ---")
        try:
            # Generate script
            script = generate_voice_script(config, patient)
            logger.info(f"Script:\n{script}\n")

            if config.get("dry_run"):
                logger.info("DRY RUN — skipping audio generation and delivery")
                delivery_result = {"status": "dry_run"}
            else:
                # Generate audio
                audio_path = generate_audio(config, script, patient)

                if audio_path:
                    # Send voicemail
                    delivery_result = send_ringless_voicemail(
                        config, patient["WirelessPhone"], audio_path
                    )
                else:
                    delivery_result = {"status": "skipped", "reason": "no_audio"}

            # Log to Open Dental
            create_commlog_entry(config, patient, script, delivery_result)

            results["patients_processed"] += 1
            if delivery_result.get("status") not in ("skipped", "dry_run"):
                results["voice_drops_sent"] += 1

            # Rate limit between patients
            time.sleep(1)

        except Exception as e:
            error_msg = f"Error processing {pat_name}: {e}"
            logger.error(error_msg, exc_info=True)
            results["errors"].append(error_msg)

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"  Patients processed: {results['patients_processed']}")
    logger.info(f"  Voice drops sent:   {results['voice_drops_sent']}")
    logger.info(f"  Errors:             {len(results['errors'])}")
    if results["errors"]:
        for err in results["errors"]:
            logger.error(f"  - {err}")
    logger.info("=" * 60)

    return results


if __name__ == "__main__":
    process_voice_drops()
