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
    """Load configuration. Prefers voice_drop_config.prod.json (gitignored,
    holds real secrets) and falls back to voice_drop_config.json (template)."""
    if config_path is None:
        base = Path(__file__).parent
        prod_path = base / "voice_drop_config.prod.json"
        config_path = prod_path if prod_path.exists() else base / "voice_drop_config.json"
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
           p.FName, p.LName, p.WirelessPhone, p.HmPhone, p.Zip
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
                "Zip": proc.get("Zip", ""),
                "procedures": [],
            }
        patient_map[pat_num]["procedures"].append({
            "ProcCode": proc.get("ProcCode", ""),
            "ProcDescription": proc.get("ProcDescription", ""),
            "ProcNum": proc.get("ProcNum"),
        })

    patients = list(patient_map.values())

    # Test mode filter — supports single name string or list of names
    if config.get("test_mode"):
        test_names_raw = config.get("test_patient_names") or [config.get("test_patient_name", "Ben Young")]
        if isinstance(test_names_raw, str):
            test_names_raw = [test_names_raw]
        allowed = set()
        for name in test_names_raw:
            parts = name.strip().split()
            if len(parts) >= 2:
                allowed.add((parts[0].lower(), parts[-1].lower()))
        patients = [
            p for p in patients
            if (p["FName"].strip().lower(), p["LName"].strip().lower()) in allowed
        ]
        logger.info(f"Test mode: filtered to {len(patients)} patient(s) matching {test_names_raw}")

    # Skip patients without phone
    skipped = [p for p in patients if not p["WirelessPhone"].strip()]
    if skipped:
        for p in skipped:
            logger.warning(f"Skipping {p['FName']} {p['LName']} (PatNum {p['PatNum']}): no phone number")
    patients = [p for p in patients if p["WirelessPhone"].strip()]

    return patients


# ─── Step 3: Determine procedure category ───

def get_procedure_category(config: dict, patient: dict) -> str:
    """Determine the primary procedure category for a patient's procedures."""
    proc_codes = [p["ProcCode"] for p in patient["procedures"]]
    categories = config.get("procedure_codes", DEFAULT_PROCEDURE_CODES)

    # Priority order: surgeries > implants > root_canals > extractions
    for category in ["surgeries", "implants", "root_canals", "extractions"]:
        codes = categories.get(category, [])
        if any(c in codes for c in proc_codes):
            return category

    return "extractions"  # fallback


# ─── Step 4: Audio Generation (splice approach) ───
#
# ElevenLabs generates ONLY a short greeting: "Hi [name]"
# This is spliced with a pre-recorded MP3 for the procedure category.
#
# Pre-recorded files go in: voice_drops/recordings/
#   extractions.mp3
#   implants.mp3
#   root_canals.mp3
#   surgeries.mp3

def generate_greeting_audio(config: dict, patient: dict) -> str:
    """Use ElevenLabs to generate just the personalized greeting."""
    if not config.get("elevenlabs_api_key"):
        logger.warning("ElevenLabs API key not configured — skipping audio generation")
        return ""

    from elevenlabs import ElevenLabs, VoiceSettings

    client = ElevenLabs(api_key=config["elevenlabs_api_key"])

    audio_dir = Path(__file__).parent / config.get("audio_output_dir", "voice_drops")
    audio_dir.mkdir(exist_ok=True)

    # Short greeting only — just the patient's name
    first_name = patient["FName"].strip().title()
    greeting_text = f"Hi {first_name}, it's Dr. Young."

    greeting_path = audio_dir / f"{patient['PatNum']}_greeting_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"

    voice_settings = VoiceSettings(
        stability=0.35,
        similarity_boost=0.80,
        style=0.3,
        use_speaker_boost=True,
    )

    audio_generator = client.text_to_speech.convert(
        text=greeting_text,
        voice_id=config["elevenlabs_voice_id"],
        model_id=config.get("elevenlabs_model_id", "eleven_multilingual_v2"),
        output_format="mp3_44100_128",
        voice_settings=voice_settings,
    )

    with open(greeting_path, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    logger.info(f"Greeting audio generated: {greeting_path} ({greeting_path.stat().st_size:,} bytes)")
    return str(greeting_path)


def get_recording_path(config: dict, category: str) -> str:
    """Get the path to the pre-recorded audio for a procedure category.
    Supports .mp3, .m4a, .wav formats."""
    recordings_dir = Path(__file__).parent / config.get("audio_output_dir", "voice_drops") / "recordings"
    for ext in [".m4a", ".mp3", ".wav"]:
        recording_path = recordings_dir / f"{category}{ext}"
        if recording_path.exists():
            return str(recording_path)
    logger.error(f"Pre-recorded audio not found for '{category}' in {recordings_dir}")
    return ""


def _trim_silence(audio, silence_thresh=-40, chunk_size=10):
    """Trim leading and trailing silence from an AudioSegment.

    Args:
        audio: pydub AudioSegment
        silence_thresh: dBFS threshold below which audio is considered silence
        chunk_size: ms granularity for detection
    Returns:
        Trimmed AudioSegment
    """
    # Trim leading silence
    start = 0
    while start < len(audio) and audio[start:start + chunk_size].dBFS < silence_thresh:
        start += chunk_size

    # Trim trailing silence
    end = len(audio)
    while end > start and audio[end - chunk_size:end].dBFS < silence_thresh:
        end -= chunk_size

    return audio[start:end]


def splice_audio(greeting_path: str, recording_path: str, output_path: str,
                 crossfade_ms: int = 80) -> str:
    """Splice the ElevenLabs greeting with a pre-recorded procedure message.

    Trims silence from both clips and crossfades them for a seamless join.
    """
    from pydub import AudioSegment

    greeting = AudioSegment.from_mp3(greeting_path)
    # Auto-detect format from extension
    rec_ext = Path(recording_path).suffix.lower().lstrip(".")
    recording = AudioSegment.from_file(recording_path, format=rec_ext)

    # Trim silence from end of greeting and start of recording
    greeting = _trim_silence(greeting)
    recording = _trim_silence(recording)

    # Normalize volume levels so they match
    greeting_loudness = greeting.dBFS
    recording_loudness = recording.dBFS
    if abs(greeting_loudness - recording_loudness) > 2:
        # Match recording volume to greeting
        recording = recording.apply_gain(greeting_loudness - recording_loudness)

    # Natural pause between greeting and message (like a breath between sentences)
    pause = AudioSegment.silent(duration=350)
    greeting_with_pause = greeting + pause

    # Crossfade the tail of greeting+pause into the start of recording
    combined = greeting_with_pause.append(recording, crossfade=crossfade_ms)

    combined.export(output_path, format="mp3", bitrate="128k")

    file_size = Path(output_path).stat().st_size
    duration_sec = len(combined) / 1000.0
    logger.info(f"Spliced audio: {output_path} ({file_size:,} bytes, {duration_sec:.1f}s)")
    return output_path


def generate_audio(config: dict, patient: dict) -> str:
    """Generate the complete voice drop audio by splicing greeting + pre-recorded message."""
    category = get_procedure_category(config, patient)
    logger.info(f"Procedure category: {category}")

    # Get pre-recorded message for this procedure type
    recording_path = get_recording_path(config, category)
    if not recording_path:
        return ""

    # Generate personalized greeting via ElevenLabs
    greeting_path = generate_greeting_audio(config, patient)
    if not greeting_path:
        return ""

    # Splice them together
    audio_dir = Path(__file__).parent / config.get("audio_output_dir", "voice_drops")
    output_path = audio_dir / f"{patient['PatNum']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"

    try:
        result = splice_audio(greeting_path, recording_path, str(output_path))
        # Clean up the temporary greeting file
        try:
            os.remove(greeting_path)
        except OSError:
            pass
        return result
    except Exception as e:
        logger.error(f"Error splicing audio: {e}", exc_info=True)
        return ""


# ─── Step 5: Voicemail Delivery ───

def send_ringless_voicemail(config: dict, phone: str, audio_path: str, patient: dict) -> dict:
    """Send audio via Drop Cowboy v1 API for ringless voicemail delivery.

    The spliced audio file is served via the MCP server's /audio/ endpoint.
    Drop Cowboy fetches it from the public URL using audio_url parameter.
    """
    if not config.get("drop_cowboy_team_id") or not config.get("drop_cowboy_secret"):
        logger.warning("Drop Cowboy credentials not configured — skipping delivery")
        return {"status": "skipped", "reason": "no_credentials"}

    # Build public URL for the audio file served by MCP server
    audio_filename = os.path.basename(audio_path)
    mcp_base = config["mcp_endpoint"].replace("/mcp", "")
    audio_url = f"{mcp_base}/audio/{audio_filename}?token={config['mcp_bearer_token']}"

    # Clean phone number to E.164 format
    clean_phone = "".join(c for c in phone if c.isdigit())
    if len(clean_phone) == 10:
        clean_phone = "1" + clean_phone
    clean_phone = f"+{clean_phone}"

    api_url = "https://api.dropcowboy.com/v1/rvm"
    headers = {
        "x-team-id": config["drop_cowboy_team_id"],
        "x-secret": config["drop_cowboy_secret"],
        "Content-Type": "application/json",
    }
    # Get patient zip for TCPA time-window compliance
    patient_zip = patient.get("Zip", "").strip() or config.get("default_postal_code", "92648")

    payload = {
        "team_id": config["drop_cowboy_team_id"],
        "secret": config["drop_cowboy_secret"],
        "phone_number": clean_phone,
        "audio_url": audio_url,
        "foreign_id": f"voicedrop-{patient['PatNum']}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "postal_code": patient_zip,
        "forwarding_number": config.get("office_phone", "+17148425035"),
    }

    resp = requests.post(api_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"Voicemail queued for {clean_phone}: {result}")
    return result


# ─── Step 6: Logging to Open Dental ───

def create_commlog_entry(config: dict, patient: dict, category: str,
                         delivery_result: dict) -> dict:
    """Create a commlog entry in Open Dental recording the voice drop."""
    proc_descriptions = ", ".join(p["ProcDescription"] for p in patient["procedures"])
    delivery_status = delivery_result.get("status", "unknown")

    first_name = patient["FName"].strip().title()
    note = (
        f"[POST-OP VOICE DROP]\n"
        f"Greeting: Hi {first_name}, it's Dr. Young.\n"
        f"Recording: {category}.mp3\n"
        f"Procedures: {proc_descriptions}\n"
        f"Phone: {patient['WirelessPhone']}\n"
        f"Delivery status: {delivery_status}"
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
            category = get_procedure_category(config, patient)
            logger.info(f"Procedure category: {category}")

            if config.get("dry_run"):
                logger.info(f"DRY RUN — would generate greeting for {patient['FName'].strip().title()} + {category}.mp3")
                delivery_result = {"status": "dry_run"}
            else:
                # Generate spliced audio (ElevenLabs greeting + pre-recorded message)
                audio_path = generate_audio(config, patient)

                if audio_path:
                    # Send voicemail
                    delivery_result = send_ringless_voicemail(
                        config, patient["WirelessPhone"], audio_path, patient
                    )
                else:
                    delivery_result = {"status": "skipped", "reason": "no_audio"}

            # Log to Open Dental
            create_commlog_entry(config, patient, category, delivery_result)

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
