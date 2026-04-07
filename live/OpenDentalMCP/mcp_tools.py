#!/usr/bin/env python3
"""
Open Dental MCP Tools
Provides tools for accessing Open Dental REST API via MCP
"""

import os
import json
import logging
import requests
import base64
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _load_env_from_mcp_config_file() -> None:
    """Optionally load environment variables from MCP_CONFIG_FILE JSON."""
    config_file = os.getenv("MCP_CONFIG_FILE", "").strip()
    if not config_file:
        return

    config_path = config_file if os.path.isabs(config_file) else os.path.join(os.getcwd(), config_file)
    if not os.path.exists(config_path):
        logger.warning("MCP_CONFIG_FILE not found: %s", config_path)
        return

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except Exception as exc:
        logger.warning("Failed loading MCP_CONFIG_FILE (%s): %s", config_path, exc)
        return

    opendental_cfg = config.get("opendental", {})
    db_cfg = config.get("database", {})
    mcp_cfg = config.get("mcp", {})
    env_map = {
        "OPENDENTAL_API_URL": opendental_cfg.get("api_url"),
        "OPENDENTAL_DEVELOPER_KEY": opendental_cfg.get("developer_key"),
        "OPENDENTAL_CUSTOMER_KEY": opendental_cfg.get("customer_key"),
        "OPENDENTAL_DB_TYPE": db_cfg.get("type"),
        "OPENDENTAL_DB_SERVER": db_cfg.get("server"),
        "OPENDENTAL_DB_DATABASE": db_cfg.get("database"),
        "OPENDENTAL_DB_USERNAME": db_cfg.get("username"),
        "OPENDENTAL_DB_PASSWORD": db_cfg.get("password"),
        "OPENDENTAL_DB_USE_WINDOWS_AUTH": db_cfg.get("use_windows_auth"),
        "OPENDENTAL_ATOZ_PATH": db_cfg.get("atoz_path", config.get("atoz_path")),
        "MCP_HTTP_PORT": mcp_cfg.get("http_port"),
        "MCP_HTTP_HOST": mcp_cfg.get("http_host"),
        "MCP_USE_HTTPS": mcp_cfg.get("use_https"),
    }
    for key, value in env_map.items():
        if value is None:
            continue
        os.environ[key] = str(value)

# Try to import database libraries (optional)
try:
    import pyodbc
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    logger.warning("pyodbc not available - database document uploads will not work")

try:
    import pymysql
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False


class OpenDentalMCPTools:
    """MCP Tools for Open Dental API"""
    
    def __init__(self):
        _load_env_from_mcp_config_file()
        self.api_url = os.getenv("OPENDENTAL_API_URL", "https://api.opendental.com/api/v1")
        self.developer_key = os.getenv("OPENDENTAL_DEVELOPER_KEY", "")
        self.customer_key = os.getenv("OPENDENTAL_CUSTOMER_KEY", "")
        
        if not self.developer_key or not self.customer_key:
            logger.warning("Open Dental API keys not configured")
        
        self.auth_header = f"ODFHIR {self.developer_key}/{self.customer_key}"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": self.auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json"
        })
        self.session.timeout = 30
        
        # Database connection settings (optional - for direct database access)
        self.db_type = os.getenv("OPENDENTAL_DB_TYPE", "").lower()  # "sqlserver" or "mysql"
        self.db_server = os.getenv("OPENDENTAL_DB_SERVER", "")
        self.db_database = os.getenv("OPENDENTAL_DB_DATABASE", "")
        self.db_username = os.getenv("OPENDENTAL_DB_USERNAME", "")
        self.db_password = os.getenv("OPENDENTAL_DB_PASSWORD", "")
        self.db_use_windows_auth = os.getenv("OPENDENTAL_DB_USE_WINDOWS_AUTH", "false").lower() == "true"
        self.atoz_path = os.getenv("OPENDENTAL_ATOZ_PATH", "")  # Path to Open Dental AtoZ folder
        
        # Query iterator settings
        self.query_iterator_max_iterations = int(os.getenv("QUERY_ITERATOR_MAX_ITERATIONS", "5"))
        self.query_iterator_timeout = int(os.getenv("QUERY_ITERATOR_TIMEOUT", "30"))
        self.query_iterator_max_rows = int(os.getenv("QUERY_ITERATOR_MAX_ROWS", "10000"))
        self.query_iterator_read_only = os.getenv("QUERY_ITERATOR_READ_ONLY", "true").lower() == "true"
    
    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None, data: Optional[Dict] = None) -> Dict:
        """Make HTTP request to Open Dental API"""
        url = f"{self.api_url}{endpoint}"
        
        try:
            if method.upper() == "GET":
                response = self.session.get(url, params=params)
            elif method.upper() == "POST":
                response = self.session.post(url, json=data, params=params)
            elif method.upper() == "PUT":
                response = self.session.put(url, json=data, params=params)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, params=params)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            # Handle empty responses
            if response.status_code == 204 or len(response.content) == 0:
                return {"success": True, "message": "Operation completed successfully"}
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    error_msg = error_data.get('message', error_data.get('error', str(e)))
                except:
                    error_msg = e.response.text or str(e)
                raise Exception(f"Open Dental API error: {error_msg}")
            raise Exception(f"Open Dental API error: {str(e)}")
    
    def list_tools(self) -> List[Dict]:
        """List all available MCP tools"""
        return [
            {
                "name": "list_resources",
                "description": "List all available Open Dental API resources/endpoints",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_patient",
                "description": "Get a patient by ID (PatNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "search_patients",
                "description": "Search for patients by criteria (last name, first name, phone, email, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "last_name": {"type": "string", "description": "Last name"},
                        "first_name": {"type": "string", "description": "First name"},
                        "phone": {"type": "string", "description": "Phone number"},
                        "email": {"type": "string", "description": "Email address"},
                        "birthdate": {"type": "string", "description": "Birthdate (YYYY-MM-DD)"},
                        "hide_inactive": {"type": "boolean", "description": "Hide inactive patients"}
                    }
                }
            },
            {
                "name": "get_appointment",
                "description": "Get an appointment by ID (AptNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "string",
                            "description": "Appointment ID (AptNum)"
                        }
                    },
                    "required": ["appointment_id"]
                }
            },
            {
                "name": "search_appointments",
                "description": "Search for appointments by criteria (patient, date, status, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "Patient ID (PatNum)"},
                        "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                        "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                        "status": {"type": "string", "description": "Appointment status"}
                    }
                }
            },
            {
                "name": "get_provider",
                "description": "Get a provider by ID (ProvNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        }
                    },
                    "required": ["provider_id"]
                }
            },
            {
                "name": "list_providers",
                "description": "List all providers",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_laboratory",
                "description": "Get a laboratory by ID (LaboratoryNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "laboratory_id": {
                            "type": "string",
                            "description": "Laboratory ID (LaboratoryNum)"
                        }
                    },
                    "required": ["laboratory_id"]
                }
            },
            {
                "name": "list_laboratories",
                "description": "List all laboratories",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_lab_cases",
                "description": "Get lab cases for a patient",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_patient_documents",
                "description": "Get documents for a patient",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_document",
                "description": "Get a document by ID (DocNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Document ID (DocNum)"
                        }
                    },
                    "required": ["document_id"]
                }
            },
            {
                "name": "get_procedure_codes",
                "description": "List procedure codes (definitions)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Limit number of results"}
                    }
                }
            },
            {
                "name": "get_statistics",
                "description": "Get practice statistics (patient count, appointment count, etc.). Note: API returns max 1000 results per request, so counts may be limited.",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_accurate_count",
                "description": "Get more accurate count for a resource by using search parameters. Use this when you need counts beyond the 1000 limit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource": {
                            "type": "string",
                            "enum": ["patients", "appointments"],
                            "description": "Resource type to count"
                        },
                        "search_params": {
                            "type": "object",
                            "description": "Search parameters to use (e.g., {'LName': 'A'} to count patients with last name starting with A)"
                        }
                    },
                    "required": ["resource"]
                }
            },
            {
                "name": "get_patient_appointments",
                "description": "Get all appointments for a specific patient",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum)"
                        },
                        "date_from": {
                            "type": "string",
                            "description": "Optional: Start date (YYYY-MM-DD)"
                        },
                        "date_to": {
                            "type": "string",
                            "description": "Optional: End date (YYYY-MM-DD)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_todays_appointments",
                "description": "Get all appointments scheduled for today",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {
                            "type": "string",
                            "description": "Optional: Filter by provider ID (ProvNum)"
                        }
                    }
                }
            },
            {
                "name": "get_upcoming_appointments",
                "description": "Get upcoming appointments within a date range",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {
                            "type": "integer",
                            "description": "Number of days ahead to look (default: 7)",
                            "default": 7
                        },
                        "provider_id": {
                            "type": "string",
                            "description": "Optional: Filter by provider ID (ProvNum)"
                        }
                    }
                }
            },
            {
                "name": "get_patient_by_phone",
                "description": "Find a patient by phone number",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "phone": {
                            "type": "string",
                            "description": "Phone number to search for"
                        }
                    },
                    "required": ["phone"]
                }
            },
            {
                "name": "get_patient_by_email",
                "description": "Find a patient by email address",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "Email address to search for"
                        }
                    },
                    "required": ["email"]
                }
            },
            {
                "name": "get_appointments_by_date_range",
                "description": "Get appointments within a specific date range",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start_date": {
                            "type": "string",
                            "description": "Start date (YYYY-MM-DD)"
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date (YYYY-MM-DD)"
                        },
                        "provider_id": {
                            "type": "string",
                            "description": "Optional: Filter by provider ID (ProvNum)"
                        },
                        "status": {
                            "type": "string",
                            "description": "Optional: Filter by appointment status"
                        }
                    },
                    "required": ["start_date", "end_date"]
                }
            },
            {
                "name": "search_patients_by_name",
                "description": "Search for patients by first and/or last name",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "first_name": {
                            "type": "string",
                            "description": "First name (partial match supported)"
                        },
                        "last_name": {
                            "type": "string",
                            "description": "Last name (partial match supported)"
                        }
                    }
                }
            },
            {
                "name": "get_provider_appointments",
                "description": "Get all appointments for a specific provider",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        },
                        "date_from": {
                            "type": "string",
                            "description": "Optional: Start date (YYYY-MM-DD)"
                        },
                        "date_to": {
                            "type": "string",
                            "description": "Optional: End date (YYYY-MM-DD)"
                        }
                    },
                    "required": ["provider_id"]
                }
            },
            {
                "name": "get_lab_case",
                "description": "Get a specific lab case by ID (LabCaseNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lab_case_id": {
                            "type": "string",
                            "description": "Lab case ID (LabCaseNum)"
                        }
                    },
                    "required": ["lab_case_id"]
                }
            },
            {
                "name": "get_procedure_code",
                "description": "Get a specific procedure code definition by code (e.g., D2740)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "procedure_code": {
                            "type": "string",
                            "description": "Procedure code (e.g., D2740, D2750)"
                        }
                    },
                    "required": ["procedure_code"]
                }
            },
            {
                "name": "search_procedure_codes",
                "description": "Search procedure codes by description or code",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "search_term": {
                            "type": "string",
                            "description": "Search term (searches in code and description)"
                        }
                    },
                    "required": ["search_term"]
                }
            },
            {
                "name": "get_patient_summary",
                "description": "Get comprehensive summary for a patient (info, appointments, lab cases, documents)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_appointment_summary",
                "description": "Get comprehensive summary for an appointment (appointment details, patient info, provider info)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "string",
                            "description": "Appointment ID (AptNum)"
                        }
                    },
                    "required": ["appointment_id"]
                }
            },
            {
                "name": "get_provider_schedule",
                "description": "Get provider's schedule for a specific date or date range",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        },
                        "date": {
                            "type": "string",
                            "description": "Date (YYYY-MM-DD) - if not provided, uses today"
                        },
                        "days": {
                            "type": "integer",
                            "description": "Number of days to include (default: 1)",
                            "default": 1
                        }
                    },
                    "required": ["provider_id"]
                }
            },
            {
                "name": "get_practice_overview",
                "description": "Get overview of practice (statistics, today's appointments, recent patients)",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "create_patient",
                "description": "Create a new patient record. WARNING: This will create a new patient in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "FName": {"type": "string", "description": "First name (required)"},
                        "LName": {"type": "string", "description": "Last name (required)"},
                        "MiddleI": {"type": "string", "description": "Middle initial"},
                        "Birthdate": {"type": "string", "description": "Birthdate (YYYY-MM-DD)"},
                        "Gender": {"type": "string", "description": "Gender (M/F)"},
                        "HmPhone": {"type": "string", "description": "Home phone"},
                        "WirelessPhone": {"type": "string", "description": "Cell phone"},
                        "Email": {"type": "string", "description": "Email address"},
                        "Address": {"type": "string", "description": "Street address"},
                        "City": {"type": "string", "description": "City"},
                        "State": {"type": "string", "description": "State"},
                        "Zip": {"type": "string", "description": "ZIP code"},
                        "SSN": {"type": "string", "description": "Social Security Number"},
                        "PatStatus": {"type": "string", "description": "Patient status"}
                    },
                    "required": ["FName", "LName"]
                }
            },
            {
                "name": "update_patient",
                "description": "Update an existing patient record. WARNING: This will modify patient data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) to update"
                        },
                        "FName": {"type": "string", "description": "First name"},
                        "LName": {"type": "string", "description": "Last name"},
                        "MiddleI": {"type": "string", "description": "Middle initial"},
                        "Birthdate": {"type": "string", "description": "Birthdate (YYYY-MM-DD)"},
                        "Gender": {"type": "string", "description": "Gender (M/F)"},
                        "HmPhone": {"type": "string", "description": "Home phone"},
                        "WirelessPhone": {"type": "string", "description": "Cell phone"},
                        "Email": {"type": "string", "description": "Email address"},
                        "Address": {"type": "string", "description": "Street address"},
                        "City": {"type": "string", "description": "City"},
                        "State": {"type": "string", "description": "State"},
                        "Zip": {"type": "string", "description": "ZIP code"},
                        "SSN": {"type": "string", "description": "Social Security Number"},
                        "PatStatus": {"type": "string", "description": "Patient status"}
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "create_appointment",
                "description": "Create a new appointment. WARNING: This will create a new appointment in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "AptDateTime": {
                            "type": "string",
                            "description": "Appointment date/time (YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD HH:MM:SS) - required"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Provider ID (ProvNum) - required"
                        },
                        "Op": {
                            "type": "string",
                            "description": "Operator/Provider ID (usually same as ProvNum) - required"
                        },
                        "AptStatus": {
                            "type": "string",
                            "description": "Appointment status (e.g., 'Scheduled', 'Arrived', 'Seated')"
                        },
                        "Pattern": {
                            "type": "string",
                            "description": "Pattern - must only be Xs and /s. Each X represents one time slot. Format: '//XXXXXXXX//' (slashes around X's is Open Dental convention). Default: '//XXXXXXXX//' (8 slots = 1 hour). Examples: '//X//' = 1 slot (7.5 min), '//XXXX//' = 4 slots (30 min), '//XXXXXXXX//' = 8 slots (1 hour)"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Appointment notes"
                        },
                        "ProcDescript": {
                            "type": "string",
                            "description": "Procedure description"
                        }
                    },
                    "required": ["PatNum", "AptDateTime", "ProvNum", "Op"]
                }
            },
            {
                "name": "update_appointment",
                "description": "Update an existing appointment. WARNING: This will modify appointment data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "string",
                            "description": "Appointment ID (AptNum) to update - required"
                        },
                        "AptDateTime": {
                            "type": "string",
                            "description": "Appointment date/time (YYYY-MM-DDTHH:MM:SS)"
                        },
                        "AptStatus": {
                            "type": "string",
                            "description": "Appointment status"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Appointment notes"
                        },
                        "ProcDescript": {
                            "type": "string",
                            "description": "Procedure description"
                        }
                    },
                    "required": ["appointment_id"]
                }
            },
            {
                "name": "create_lab_case",
                "description": "Create a new lab case. WARNING: This will create a new lab case in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "LaboratoryNum": {
                            "type": "string",
                            "description": "Laboratory ID (LaboratoryNum) - required"
                        },
                        "AptNum": {
                            "type": "string",
                            "description": "Appointment ID (AptNum) - optional"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Provider ID (ProvNum) - required"
                        },
                        "DateTimeSent": {
                            "type": "string",
                            "description": "Date/time sent (YYYY-MM-DDTHH:MM:SS)"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Lab case notes"
                        }
                    },
                    "required": ["PatNum", "LaboratoryNum", "ProvNum"]
                }
            },
            {
                "name": "update_lab_case",
                "description": "Update an existing lab case. WARNING: This will modify lab case data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lab_case_id": {
                            "type": "string",
                            "description": "Lab case ID (LabCaseNum) to update - required"
                        },
                        "DateTimeSent": {
                            "type": "string",
                            "description": "Date/time sent (YYYY-MM-DDTHH:MM:SS)"
                        },
                        "DateTimeReceived": {
                            "type": "string",
                            "description": "Date/time received (YYYY-MM-DDTHH:MM:SS)"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Lab case notes"
                        }
                    },
                    "required": ["lab_case_id"]
                }
            },
            {
                "name": "update_procedure_log",
                "description": "Update an existing procedure log (procedure code). WARNING: This will modify procedure data in Open Dental. Cannot update completed procedures (ProcStatus = C). Treatment areas must match when changing codes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "procedure_id": {
                            "type": "string",
                            "description": "Procedure ID (ProcNum) to update - required"
                        },
                        "procCode": {
                            "type": "string",
                            "description": "Procedure code (e.g., D1110, D2740) - alternative to CodeNum"
                        },
                        "CodeNum": {
                            "type": "string",
                            "description": "Procedure code internal ID (CodeNum) - alternative to procCode"
                        }
                    },
                    "required": ["procedure_id"]
                }
            },
            {
                "name": "create_procedure_log",
                "description": "Create a new procedure log entry (treatment planned procedure). WARNING: This will create a new procedure in Open Dental. Use ProcStatus 'TP' to add treatment-planned procedures that appear on the patient's active treatment plan. Surfaces should use standard abbreviations: M=Mesial, D=Distal, F=Facial, L=Lingual, O=Occlusal, I=Incisal, B=Buccal.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "procCode": {
                            "type": "string",
                            "description": "Procedure code (e.g., D6010, D2393, D7951) - required"
                        },
                        "ToothNum": {
                            "type": "string",
                            "description": "Optional: Tooth number (1-32 for permanent, A-T for primary)"
                        },
                        "Surf": {
                            "type": "string",
                            "description": "Optional: Tooth surfaces (e.g., 'MFL', 'DO', 'MODBL'). Use standard abbreviations."
                        },
                        "ProcDate": {
                            "type": "string",
                            "description": "Optional: Procedure date (YYYY-MM-DD). Defaults to today."
                        },
                        "ProcStatus": {
                            "type": "string",
                            "description": "Optional: Procedure status. 'TP' = Treatment Planned (default), 'C' = Complete, 'EC' = Existing Current, 'EO' = Existing Other, 'R' = Referred, 'D' = Deleted, 'Cn' = Condition"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Optional: Provider ID (ProvNum). Defaults to patient's primary provider."
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (ClinicNum)"
                        },
                        "Dx": {
                            "type": "string",
                            "description": "Optional: Diagnosis definition ID (DefNum)"
                        },
                        "priority": {
                            "type": "string",
                            "description": "Optional: Treatment priority definition ID (DefNum)"
                        },
                        "ToothRange": {
                            "type": "string",
                            "description": "Optional: Tooth range for procedures spanning multiple teeth (e.g., '1-16')"
                        },
                        "ProcFee": {
                            "type": "string",
                            "description": "Optional: Procedure fee override. If not set, uses the fee schedule amount."
                        }
                    },
                    "required": ["PatNum", "procCode"]
                }
            },
            {
                "name": "create_document",
                "description": "Create a new document record. Uses direct database access if configured (set OPENDENTAL_DB_* environment variables). If database not configured, will attempt REST API (which may not work).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "FileName": {
                            "type": "string",
                            "description": "File name - required"
                        },
                        "Description": {
                            "type": "string",
                            "description": "Document description"
                        },
                        "DocCategory": {
                            "type": "integer",
                            "description": "Document category (default: 0)"
                        },
                        "DateCreated": {
                            "type": "string",
                            "description": "Date created (YYYY-MM-DD, defaults to today)"
                        }
                    },
                    "required": ["PatNum", "FileName"]
                }
            },
            {
                "name": "create_procnote",
                "description": "Write a clinical note for a completed procedure in Open Dental. Inserts into the procnote table linked to a specific ProcNum. Use for documenting procedure notes after treatment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "proc_num": {
                            "type": "integer",
                            "description": "ProcNum from procedurelog — the procedure to attach the note to"
                        },
                        "pat_num": {
                            "type": "integer",
                            "description": "PatNum of the patient"
                        },
                        "note": {
                            "type": "string",
                            "description": "The clinical note text"
                        },
                        "user_num": {
                            "type": "integer",
                            "description": "UserNum of the provider writing the note (default: 0)",
                            "default": 0
                        }
                    },
                    "required": ["proc_num", "pat_num", "note"]
                }
            },
            {
                "name": "query_database",
                "description": "Execute a query against Open Dental. First tries to use REST API endpoints when possible, then falls back to direct database access for complex queries. For simple queries, uses API (no database config needed). For complex SQL queries, requires OPENDENTAL_DB_* environment variables.\n\nCRITICAL SCHEMA NOTES FOR ACCURATE QUERIES:\n\n1. CLINICAL NOTES (~GRP~ group notes):\n   - Notes are stored in `procnote` table, linked to `procedurelog` via ProcNum\n   - Providers frequently use GROUP NOTES instead of per-procedure notes\n   - A group note is a procedurelog row where procedurecode.ProcCode = '~GRP~', \n     with AptNum=0, keyed by PatNum + ProcDate\n   - A group note with ProcStatus=3 and at least one procnote entry covers ALL \n     procedures for that patient on that date\n   - ProcStatus=6 means the group note was deleted — do NOT count it\n   - When checking for missing notes, a procedure is covered if EITHER:\n     (a) It has a direct procnote (procnote.ProcNum = procedurelog.ProcNum), OR\n     (b) The patient has a ~GRP~ procedurelog row on the same ProcDate with \n         ProcStatus=3 that has at least one procnote attached\n   - Always apply BOTH checks or queries will generate false positives\n\n2. PROCEDURE STATUS VALUES:\n   - ProcStatus=1: Treatment Planned\n   - ProcStatus=2: Complete\n   - ProcStatus=3: Existing (used for group notes)\n   - ProcStatus=6: Deleted\n\n3. CODES TO EXCLUDE from clinical note audits (don't require notes):\n   - Hygiene: D1110, D1120, D1206, D1208, D4910\n   - Radiology: D0210, D0220, D0230, D0240, D0270, D0272, D0273, D0274, D0277, D0330\n   - Admin/other: SEAT, N4101, DR001, FAIL, D8670, D9986, D9987",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Query to execute. Can be: 1) Natural language query (e.g., 'get patients with appointments next week'), 2) SQL query (e.g., 'SELECT * FROM patient WHERE PatNum = 123'), or 3) API endpoint path (e.g., '/patients?PatNum=123'). For SQL queries, requires database configuration."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of rows to return (default: 1000, max: 10000)",
                            "default": 1000
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "smart_query",
                "description": "Self-iterating query tool that automatically generates, executes, troubleshoots, and fixes SQL queries from natural language descriptions. Best for simple single-table queries. Automatically detects complex queries (requiring JOINs or aggregations) and fails fast with helpful SQL suggestions. For complex queries, use query_database tool directly with SQL. Automatically handles syntax errors, schema issues, and iterates up to 5 times to produce a working query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Natural language description of the query. Works best for simple queries like 'Find patients named Smith' or 'Get today's appointments'. Complex queries requiring JOINs (e.g., 'Find patients with appointments next week') will be detected and return suggested SQL instead of executing."
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "Maximum number of iterations to attempt (default: 5)",
                            "default": 5
                        },
                        "read_only": {
                            "type": "boolean",
                            "description": "Only allow SELECT queries (default: true). Set to false to allow INSERT/UPDATE/DELETE (not recommended)",
                            "default": True
                        },
                        "validate_results": {
                            "type": "boolean",
                            "description": "Validate that results match the query intent (default: true)",
                            "default": True
                        },
                        "schema_hints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional hints about table/column names to help query generation"
                        }
                    },
                    "required": ["description"]
                }
            },
            {
                "name": "upload_document",
                "description": "Upload a document file (base64 encoded) to Open Dental. Uses direct database access if configured (set OPENDENTAL_DB_* environment variables). If database not configured, will attempt REST API (which may not work).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "file_name": {
                            "type": "string",
                            "description": "File name - required"
                        },
                        "file_data": {
                            "type": "string",
                            "description": "Base64 encoded file data - required"
                        },
                        "description": {
                            "type": "string",
                            "description": "Document description"
                        },
                        "category": {
                            "type": "integer",
                            "description": "Document category (default: 0)"
                        }
                    },
                    "required": ["patient_id", "file_name", "file_data"]
                }
            },
            {
                "name": "get_patient_aging",
                "description": "Get aging information for a patient and their family (Account Module Aging grid)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_patient_balances",
                "description": "Get patient portion balances for a patient's family (Account Module Select Patient grid)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_patient_service_date_view",
                "description": "Get list of all charges and credits for a patient and their family (Service Date View)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "is_family": {
                            "type": "boolean",
                            "description": "Return data for entire family (default: false)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_adjustments",
                "description": "Get all adjustments for a patient",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "adj_type": {
                            "type": "string",
                            "description": "Optional: Adjustment type (DefNum where Category=1)"
                        },
                        "proc_num": {
                            "type": "string",
                            "description": "Optional: Procedure number (ProcNum)"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "create_adjustment",
                "description": "Create a new adjustment for a patient. WARNING: This will create a new adjustment in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "AdjType": {
                            "type": "string",
                            "description": "Adjustment type (DefNum where Category=1) - required"
                        },
                        "AdjAmt": {
                            "type": "number",
                            "description": "Adjustment amount - required. Must be positive if AdjType is '+', negative if '-'"
                        },
                        "AdjDate": {
                            "type": "string",
                            "description": "Adjustment date (YYYY-MM-DD) - required. Cannot be future date"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Optional: Provider ID (default: patient.PriProv)"
                        },
                        "ProcNum": {
                            "type": "string",
                            "description": "Optional: Procedure number to attach adjustment to"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (default: patient.ClinicNum)"
                        },
                        "ProcDate": {
                            "type": "string",
                            "description": "Optional: Procedure date (YYYY-MM-DD)"
                        },
                        "AdjNote": {
                            "type": "string",
                            "description": "Optional: Adjustment note"
                        }
                    },
                    "required": ["PatNum", "AdjType", "AdjAmt", "AdjDate"]
                }
            },
            {
                "name": "update_adjustment",
                "description": "Update an existing adjustment. WARNING: This will modify adjustment data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "adjustment_id": {
                            "type": "string",
                            "description": "Adjustment ID (AdjNum) to update - required"
                        },
                        "AdjDate": {
                            "type": "string",
                            "description": "Adjustment date (YYYY-MM-DD). Cannot be future date"
                        },
                        "AdjAmt": {
                            "type": "number",
                            "description": "Adjustment amount"
                        },
                        "AdjType": {
                            "type": "string",
                            "description": "Adjustment type (DefNum where Category=1)"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        },
                        "AdjNote": {
                            "type": "string",
                            "description": "Adjustment note (overwrites existing)"
                        },
                        "ProcNum": {
                            "type": "string",
                            "description": "Procedure number to attach adjustment to"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Clinic ID (ClinicNum)"
                        }
                    },
                    "required": ["adjustment_id"]
                }
            },
            {
                "name": "get_allergies",
                "description": "Get all allergies for a patient",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "create_allergy",
                "description": "Create a new allergy record for a patient. WARNING: This will create a new allergy in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "defDescription": {
                            "type": "string",
                            "description": "Allergy description (preferred). Either defDescription or AllergyDefNum is required"
                        },
                        "AllergyDefNum": {
                            "type": "string",
                            "description": "Allergy definition ID (AllergyDefNum). Either defDescription or AllergyDefNum is required"
                        },
                        "Reaction": {
                            "type": "string",
                            "description": "Optional: Reaction description"
                        },
                        "StatusIsActive": {
                            "type": "boolean",
                            "description": "Optional: Status is active (default: true)"
                        },
                        "DateAdverseReaction": {
                            "type": "string",
                            "description": "Optional: Date of adverse reaction (YYYY-MM-DD)"
                        },
                        "AdverseReactionCode": {
                            "type": "string",
                            "description": "Optional: Adverse reaction code"
                        },
                        "Severity": {
                            "type": "string",
                            "description": "Optional: Severity (e.g., 'Mild', 'Moderate', 'Severe')"
                        }
                    },
                    "required": ["PatNum"]
                }
            },
            {
                "name": "update_allergy",
                "description": "Update an existing allergy record. WARNING: This will modify allergy data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "allergy_id": {
                            "type": "string",
                            "description": "Allergy ID (AllergyNum) to update - required"
                        },
                        "AllergyDefNum": {
                            "type": "string",
                            "description": "Allergy definition ID (AllergyDefNum)"
                        },
                        "Reaction": {
                            "type": "string",
                            "description": "Reaction description"
                        },
                        "AdverseReactionCode": {
                            "type": "string",
                            "description": "Adverse reaction code"
                        },
                        "Severity": {
                            "type": "string",
                            "description": "Severity (e.g., 'Mild', 'Moderate', 'Severe')"
                        }
                    },
                    "required": ["allergy_id"]
                }
            },
            {
                "name": "delete_allergy",
                "description": "Delete an allergy record. WARNING: This will permanently delete the allergy from Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "allergy_id": {
                            "type": "string",
                            "description": "Allergy ID (AllergyNum) to delete - required"
                        }
                    },
                    "required": ["allergy_id"]
                }
            },
            {
                "name": "get_procedure_log",
                "description": "Get a procedure log by ID (ProcNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "procedure_id": {
                            "type": "string",
                            "description": "Procedure ID (ProcNum) - required"
                        }
                    },
                    "required": ["procedure_id"]
                }
            },
            {
                "name": "get_procedure_logs",
                "description": "Get procedure logs for a patient or by criteria",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "appointment_id": {
                            "type": "string",
                            "description": "Optional: Appointment ID (AptNum)"
                        },
                        "proc_status": {
                            "type": "string",
                            "description": "Optional: Procedure status (e.g., 'TP', 'C', 'EO')"
                        }
                    }
                }
            },
            {
                "name": "get_claim",
                "description": "Get a single claim by ID (ClaimNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_id": {
                            "type": "string",
                            "description": "Claim ID (ClaimNum) - required"
                        }
                    },
                    "required": ["claim_id"]
                }
            },
            {
                "name": "get_claims",
                "description": "Get claims by criteria (patient, status, date edited)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "claim_status": {
                            "type": "string",
                            "description": "Optional: Claim status ('U'=Unsent, 'H'=Hold, 'W'=Waiting, 'S'=Sent, 'R'=Received, 'I'=In Process)"
                        },
                        "sec_date_t_edit": {
                            "type": "string",
                            "description": "Optional: Last edited date (YYYY-MM-DD HH:MM:SS). Returns claims on or after this date"
                        }
                    }
                }
            },
            {
                "name": "create_claim",
                "description": "Create a new claim. WARNING: This will create a new claim in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "procNums": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of procedure numbers (ProcNums) to attach to claim - required"
                        },
                        "ClaimType": {
                            "type": "string",
                            "description": "Claim type ('P'=Primary, 'S'=Secondary, 'PreAuth'=Preauthorization) - required"
                        },
                        "InsSubNum": {
                            "type": "string",
                            "description": "Required for PreAuth: Insurance subscription ID (InsSubNum)"
                        },
                        "InsSubNum2": {
                            "type": "string",
                            "description": "Optional: Other coverage insurance subscription ID"
                        },
                        "PatRelat": {
                            "type": "string",
                            "description": "Required for PreAuth: Patient relationship ('Self', 'Spouse', 'Child', etc.)"
                        },
                        "PatRelat2": {
                            "type": "string",
                            "description": "Optional: Patient relationship for other coverage"
                        },
                        "DateService": {
                            "type": "string",
                            "description": "Optional: Service date (YYYY-MM-DD). Defaults to earliest procedure date"
                        },
                        "DateSent": {
                            "type": "string",
                            "description": "Optional: Date sent (YYYY-MM-DD). Defaults to today"
                        },
                        "ClaimForm": {
                            "type": "string",
                            "description": "Optional: Claim form ID (ClaimFormNum)"
                        },
                        "ProvTreat": {
                            "type": "string",
                            "description": "Optional: Treating provider ID (ProvNum)"
                        },
                        "ProvBill": {
                            "type": "string",
                            "description": "Optional: Billing provider ID (ProvNum)"
                        }
                    },
                    "required": ["PatNum", "procNums", "ClaimType"]
                }
            },
            {
                "name": "update_claim",
                "description": "Update an existing claim. WARNING: This will modify claim data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_id": {
                            "type": "string",
                            "description": "Claim ID (ClaimNum) to update - required"
                        },
                        "ClaimStatus": {
                            "type": "string",
                            "description": "Claim status ('U'=Unsent, 'H'=Hold, 'W'=Waiting, 'S'=Sent, 'R'=Received)"
                        },
                        "DateReceived": {
                            "type": "string",
                            "description": "Date received (YYYY-MM-DD)"
                        },
                        "ProvTreat": {
                            "type": "string",
                            "description": "Treating provider ID (ProvNum)"
                        },
                        "IsProsthesis": {
                            "type": "string",
                            "description": "Prosthesis status ('N'=No, 'I'=Initial, 'R'=Replacement)"
                        },
                        "PriorDate": {
                            "type": "string",
                            "description": "Prior prosthesis date (YYYY-MM-DD)"
                        },
                        "ClaimNote": {
                            "type": "string",
                            "description": "Claim note (overwrites existing)"
                        },
                        "ReasonUnderPaid": {
                            "type": "string",
                            "description": "Reason underpaid note (overwrites existing)"
                        },
                        "ProvBill": {
                            "type": "string",
                            "description": "Billing provider ID (ProvNum)"
                        },
                        "PlaceService": {
                            "type": "string",
                            "description": "Service location (usually 'Office')"
                        },
                        "AccidentRelated": {
                            "type": "string",
                            "description": "Accident type ('No', 'A'=Auto, 'E'=Employment, 'O'=Other)"
                        },
                        "AccidentDate": {
                            "type": "string",
                            "description": "Accident date (YYYY-MM-DD)"
                        },
                        "AccidentST": {
                            "type": "string",
                            "description": "Accident state (2 characters)"
                        },
                        "IsOrtho": {
                            "type": "boolean",
                            "description": "Is orthodontic treatment"
                        },
                        "OrthoRemainM": {
                            "type": "integer",
                            "description": "Remaining months of ortho (1-36)"
                        },
                        "OrthoDate": {
                            "type": "string",
                            "description": "Ortho appliance placement date (YYYY-MM-DD)"
                        },
                        "OrthoTotalM": {
                            "type": "integer",
                            "description": "Estimated total months of ortho (1-36)"
                        }
                    },
                    "required": ["claim_id"]
                }
            },
            {
                "name": "get_claim_proc",
                "description": "Get a single claim procedure by ID (ClaimProcNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_proc_id": {
                            "type": "string",
                            "description": "Claim procedure ID (ClaimProcNum) - required"
                        }
                    },
                    "required": ["claim_proc_id"]
                }
            },
            {
                "name": "get_claim_procs",
                "description": "Get claim procedures by criteria (procedure, claim, patient, status, payment)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "proc_num": {
                            "type": "string",
                            "description": "Optional: Procedure number (ProcNum)"
                        },
                        "claim_num": {
                            "type": "string",
                            "description": "Optional: Claim number (ClaimNum)"
                        },
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "status": {
                            "type": "string",
                            "description": "Optional: Status ('NotReceived', 'Received', 'Preauth', 'Supplemental', 'Estimate', etc.)"
                        },
                        "claim_payment_num": {
                            "type": "string",
                            "description": "Optional: Claim payment number (ClaimPaymentNum)"
                        }
                    }
                }
            },
            {
                "name": "update_claim_proc",
                "description": "Update a claim procedure. WARNING: This will modify claim procedure data in Open Dental. Complex operation - see API docs for details.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_proc_id": {
                            "type": "string",
                            "description": "Claim procedure ID (ClaimProcNum) to update - required"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Provider ID (ProvNum)"
                        },
                        "FeeBilled": {
                            "type": "number",
                            "description": "Amount billed to insurance"
                        },
                        "DedApplied": {
                            "type": "number",
                            "description": "Deductible applied to this procedure"
                        },
                        "Status": {
                            "type": "string",
                            "description": "Status ('NotReceived', 'Received', 'Preauth', 'Supplemental', 'Estimate')"
                        },
                        "InsPayAmt": {
                            "type": "number",
                            "description": "Amount insurance actually paid"
                        },
                        "Remarks": {
                            "type": "string",
                            "description": "Remarks from insurance EOB (overwrites existing)"
                        },
                        "ClaimPaymentNum": {
                            "type": "string",
                            "description": "Claim payment number (ClaimPaymentNum) for partial payment"
                        },
                        "WriteOff": {
                            "type": "number",
                            "description": "Amount written off"
                        },
                        "CodeSent": {
                            "type": "string",
                            "description": "Procedure code sent to insurance"
                        },
                        "PercentOverride": {
                            "type": "integer",
                            "description": "Percentage override (0-100, use -1 for none)"
                        },
                        "NoBillIns": {
                            "type": "boolean",
                            "description": "Do not bill to insurance"
                        },
                        "CopayOverride": {
                            "type": "number",
                            "description": "Copay override (use -1 for none)"
                        },
                        "DedEstOverride": {
                            "type": "number",
                            "description": "Deductible estimate override (use -1 for none)"
                        },
                        "InsEstTotalOverride": {
                            "type": "number",
                            "description": "Insurance estimate total override (use -1 for none)"
                        },
                        "PaidOtherInsOverride": {
                            "type": "number",
                            "description": "Paid by other insurance override (use -1 for none)"
                        },
                        "WriteOffEstOverride": {
                            "type": "number",
                            "description": "Write-off estimate override (use -1 for none)"
                        },
                        "ClaimPaymentTracking": {
                            "type": "string",
                            "description": "Claim payment tracking (DefNum where Category=36)"
                        }
                    },
                    "required": ["claim_proc_id"]
                }
            },
            {
                "name": "get_claim_payment",
                "description": "Get a single claim payment by ID (ClaimPaymentNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_payment_id": {
                            "type": "string",
                            "description": "Claim payment ID (ClaimPaymentNum) - required"
                        }
                    },
                    "required": ["claim_payment_id"]
                }
            },
            {
                "name": "get_claim_payments",
                "description": "Get claim payments by criteria (date edited)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sec_date_t_edit": {
                            "type": "string",
                            "description": "Optional: Last edited date (YYYY-MM-DD HH:MM:SS). Returns payments on or after this date"
                        }
                    }
                }
            },
            {
                "name": "create_claim_payment",
                "description": "Create a new claim payment. WARNING: This will create a new claim payment in Open Dental. Prior to this, update ClaimProcs Status and InsPayAmt, and update Claim ClaimStatus to 'R'.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claimNum": {
                            "type": "string",
                            "description": "Claim number (ClaimNum) receiving payment - required"
                        },
                        "CheckAmt": {
                            "type": "number",
                            "description": "Check amount - required"
                        },
                        "CheckDate": {
                            "type": "string",
                            "description": "Optional: Date check entered (YYYY-MM-DD). Defaults to today"
                        },
                        "CheckNum": {
                            "type": "string",
                            "description": "Optional: Check number"
                        },
                        "BankBranch": {
                            "type": "string",
                            "description": "Optional: Bank and branch"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Note for this check"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (defaults to Claim ClinicNum)"
                        },
                        "CarrierName": {
                            "type": "string",
                            "description": "Optional: Carrier name (defaults to InsPlan CarrierName)"
                        },
                        "DateIssued": {
                            "type": "string",
                            "description": "Optional: Date carrier issued check (YYYY-MM-DD)"
                        },
                        "PayType": {
                            "type": "string",
                            "description": "Optional: Payment type (DefNum where Category=32)"
                        },
                        "PayGroup": {
                            "type": "string",
                            "description": "Optional: Payment group (DefNum where Category=40)"
                        }
                    },
                    "required": ["claimNum", "CheckAmt"]
                }
            },
            {
                "name": "create_claim_payment_batch",
                "description": "Create a batch claim payment for multiple claims. WARNING: This will create claim payments in Open Dental. Prior to this, update ClaimProcs Status and InsPayAmt, and update Claims ClaimStatus to 'R'.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claimNums": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of claim numbers (ClaimNums) receiving payment - required"
                        },
                        "CheckAmt": {
                            "type": "number",
                            "description": "Check amount - required"
                        },
                        "CheckDate": {
                            "type": "string",
                            "description": "Optional: Date check entered (YYYY-MM-DD). Defaults to today"
                        },
                        "CheckNum": {
                            "type": "string",
                            "description": "Optional: Check number"
                        },
                        "BankBranch": {
                            "type": "string",
                            "description": "Optional: Bank and branch"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Note for this payment"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (defaults to 0)"
                        },
                        "CarrierName": {
                            "type": "string",
                            "description": "Optional: Carrier name"
                        },
                        "DateIssued": {
                            "type": "string",
                            "description": "Optional: Date carrier issued payment (YYYY-MM-DD)"
                        },
                        "PayType": {
                            "type": "string",
                            "description": "Optional: Payment type (DefNum where Category=32)"
                        },
                        "PayGroup": {
                            "type": "string",
                            "description": "Optional: Payment group (DefNum where Category=40)"
                        }
                    },
                    "required": ["claimNums", "CheckAmt"]
                }
            },
            {
                "name": "update_claim_payment",
                "description": "Update an existing claim payment. WARNING: This will modify claim payment data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_payment_id": {
                            "type": "string",
                            "description": "Claim payment ID (ClaimPaymentNum) to update - required"
                        },
                        "CheckAmt": {
                            "type": "number",
                            "description": "Check amount"
                        },
                        "CheckNum": {
                            "type": "string",
                            "description": "Check number"
                        },
                        "BankBranch": {
                            "type": "string",
                            "description": "Bank and branch"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Note (overwrites existing)"
                        },
                        "CarrierName": {
                            "type": "string",
                            "description": "Carrier name"
                        },
                        "PayType": {
                            "type": "string",
                            "description": "Payment type (DefNum where Category=32)"
                        },
                        "PayGroup": {
                            "type": "string",
                            "description": "Payment group (DefNum where Category=40)"
                        }
                    },
                    "required": ["claim_payment_id"]
                }
            },
            {
                "name": "delete_claim_payment",
                "description": "Delete a claim payment. WARNING: This will permanently delete the claim payment from Open Dental. Cannot delete if associated with EOB or deposit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim_payment_id": {
                            "type": "string",
                            "description": "Claim payment ID (ClaimPaymentNum) to delete - required"
                        }
                    },
                    "required": ["claim_payment_id"]
                }
            },
            {
                "name": "get_insurance_plan",
                "description": "Get a single insurance plan by ID (PlanNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_id": {
                            "type": "string",
                            "description": "Insurance plan ID (PlanNum) - required"
                        }
                    },
                    "required": ["plan_id"]
                }
            },
            {
                "name": "get_insurance_plans",
                "description": "Get insurance plans by criteria (plan type, carrier)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_type": {
                            "type": "string",
                            "description": "Optional: Plan type ('percentage', 'p'=PPO, 'f'=Flat Copay, 'c'=Capitation)"
                        },
                        "carrier_num": {
                            "type": "string",
                            "description": "Optional: Carrier ID (CarrierNum)"
                        }
                    }
                }
            },
            {
                "name": "create_insurance_plan",
                "description": "Create a new insurance plan. WARNING: This will create a new insurance plan in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "CarrierNum": {
                            "type": "string",
                            "description": "Carrier ID (CarrierNum) - required"
                        },
                        "GroupName": {
                            "type": "string",
                            "description": "Optional: Group name (typically same as employer)"
                        },
                        "GroupNum": {
                            "type": "string",
                            "description": "Optional: Plan number (Canada)"
                        },
                        "PlanNote": {
                            "type": "string",
                            "description": "Optional: Plan note"
                        },
                        "FeeSched": {
                            "type": "string",
                            "description": "Optional: Fee schedule ID (FeeSchedNum)"
                        },
                        "PlanType": {
                            "type": "string",
                            "description": "Optional: Plan type ('', 'p', 'f', 'c'). Default '' (Percentage)"
                        },
                        "ClaimFormNum": {
                            "type": "string",
                            "description": "Optional: Claim form ID (ClaimFormNum)"
                        },
                        "ClaimsUseUCR": {
                            "type": "boolean",
                            "description": "Optional: Use UCR on claims"
                        },
                        "CopayFeeSched": {
                            "type": "string",
                            "description": "Optional: Copay fee schedule ID (FeeSchedNum)"
                        },
                        "EmployerNum": {
                            "type": "string",
                            "description": "Optional: Employer ID (EmployerNum)"
                        },
                        "IsMedical": {
                            "type": "boolean",
                            "description": "Optional: Is medical plan"
                        },
                        "FilingCode": {
                            "type": "string",
                            "description": "Optional: Filing code ID (InsFilingCodeNum)"
                        },
                        "ShowBaseUnits": {
                            "type": "boolean",
                            "description": "Optional: Show base units"
                        },
                        "CodeSubstNone": {
                            "type": "boolean",
                            "description": "Optional: Ignore substitution codes"
                        },
                        "IsHidden": {
                            "type": "boolean",
                            "description": "Optional: Is hidden"
                        },
                        "MonthRenew": {
                            "type": "integer",
                            "description": "Optional: Renewal month (1-12, 0=calendar year)"
                        },
                        "FilingCodeSubtype": {
                            "type": "string",
                            "description": "Optional: Filing code subtype ID"
                        },
                        "CobRule": {
                            "type": "string",
                            "description": "Optional: COB rule ('Basic', 'Standard', 'CarveOut', 'SecondaryMedicaid')"
                        },
                        "BillingType": {
                            "type": "string",
                            "description": "Optional: Billing type (DefNum where Category=4)"
                        },
                        "ExclusionFeeRule": {
                            "type": "string",
                            "description": "Optional: Exclusion fee rule ('PracticeDefault', 'DoNothing', 'UseUcrFee')"
                        },
                        "ManualFeeSchedNum": {
                            "type": "string",
                            "description": "Optional: Manual fee schedule ID (FeeSchedNum)"
                        },
                        "IsBlueBookEnabled": {
                            "type": "boolean",
                            "description": "Optional: Is BlueBook enabled"
                        },
                        "InsPlansZeroWriteOffsOnAnnualMaxOverride": {
                            "type": "string",
                            "description": "Optional: Zero write-offs override ('Default', 'Yes', 'No')"
                        }
                    },
                    "required": ["CarrierNum"]
                }
            },
            {
                "name": "update_insurance_plan",
                "description": "Update an existing insurance plan. WARNING: This will modify insurance plan data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_id": {
                            "type": "string",
                            "description": "Insurance plan ID (PlanNum) to update - required"
                        },
                        "GroupName": {
                            "type": "string",
                            "description": "Group name"
                        },
                        "GroupNum": {
                            "type": "string",
                            "description": "Plan number (Canada)"
                        },
                        "PlanNote": {
                            "type": "string",
                            "description": "Plan note"
                        },
                        "FeeSched": {
                            "type": "string",
                            "description": "Fee schedule ID (FeeSchedNum)"
                        },
                        "PlanType": {
                            "type": "string",
                            "description": "Plan type ('', 'p', 'f', 'c')"
                        },
                        "ClaimFormNum": {
                            "type": "string",
                            "description": "Claim form ID (ClaimFormNum)"
                        },
                        "ClaimsUseUCR": {
                            "type": "boolean",
                            "description": "Use UCR on claims"
                        },
                        "CopayFeeSched": {
                            "type": "string",
                            "description": "Copay fee schedule ID (FeeSchedNum)"
                        },
                        "EmployerNum": {
                            "type": "string",
                            "description": "Employer ID (EmployerNum)"
                        },
                        "CarrierNum": {
                            "type": "string",
                            "description": "Carrier ID (CarrierNum)"
                        },
                        "IsMedical": {
                            "type": "boolean",
                            "description": "Is medical plan"
                        },
                        "FilingCode": {
                            "type": "string",
                            "description": "Filing code ID (InsFilingCodeNum)"
                        },
                        "ShowBaseUnits": {
                            "type": "boolean",
                            "description": "Show base units"
                        },
                        "CodeSubstNone": {
                            "type": "boolean",
                            "description": "Ignore substitution codes"
                        },
                        "IsHidden": {
                            "type": "boolean",
                            "description": "Is hidden"
                        },
                        "MonthRenew": {
                            "type": "integer",
                            "description": "Renewal month (1-12, 0=calendar year)"
                        },
                        "FilingCodeSubtype": {
                            "type": "string",
                            "description": "Filing code subtype ID"
                        },
                        "CobRule": {
                            "type": "string",
                            "description": "COB rule ('Basic', 'Standard', 'CarveOut', 'SecondaryMedicaid')"
                        },
                        "BillingType": {
                            "type": "string",
                            "description": "Billing type (DefNum where Category=4)"
                        },
                        "ExclusionFeeRule": {
                            "type": "string",
                            "description": "Exclusion fee rule ('PracticeDefault', 'DoNothing', 'UseUcrFee')"
                        },
                        "ManualFeeSchedNum": {
                            "type": "string",
                            "description": "Manual fee schedule ID (FeeSchedNum)"
                        },
                        "IsBlueBookEnabled": {
                            "type": "boolean",
                            "description": "Is BlueBook enabled"
                        },
                        "InsPlansZeroWriteOffsOnAnnualMaxOverride": {
                            "type": "string",
                            "description": "Zero write-offs override ('Default', 'Yes', 'No')"
                        }
                    },
                    "required": ["plan_id"]
                }
            },
            {
                "name": "get_insurance_subscription",
                "description": "Get a single insurance subscription by ID (InsSubNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ins_sub_id": {
                            "type": "string",
                            "description": "Insurance subscription ID (InsSubNum) - required"
                        }
                    },
                    "required": ["ins_sub_id"]
                }
            },
            {
                "name": "get_insurance_subscriptions",
                "description": "Get insurance subscriptions by criteria (plan, subscriber, date edited)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_num": {
                            "type": "string",
                            "description": "Optional: Insurance plan ID (PlanNum)"
                        },
                        "subscriber": {
                            "type": "string",
                            "description": "Optional: Subscriber patient ID (PatNum)"
                        },
                        "sec_date_t_edit": {
                            "type": "string",
                            "description": "Optional: Last edited date (YYYY-MM-DD HH:MM:SS). Returns subscriptions on or after this date"
                        }
                    }
                }
            },
            {
                "name": "create_insurance_subscription",
                "description": "Create a new insurance subscription. WARNING: This will create a new insurance subscription in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PlanNum": {
                            "type": "string",
                            "description": "Insurance plan ID (PlanNum) - required"
                        },
                        "Subscriber": {
                            "type": "string",
                            "description": "Subscriber patient ID (PatNum) - required"
                        },
                        "SubscriberID": {
                            "type": "string",
                            "description": "Subscriber ID assigned by insurance company - required"
                        },
                        "DateEffective": {
                            "type": "string",
                            "description": "Optional: Date plan became effective (YYYY-MM-DD)"
                        },
                        "DateTerm": {
                            "type": "string",
                            "description": "Optional: Date plan was terminated (YYYY-MM-DD)"
                        },
                        "BenefitNotes": {
                            "type": "string",
                            "description": "Optional: Benefit notes (for automated notes)"
                        },
                        "ReleaseInfo": {
                            "type": "boolean",
                            "description": "Optional: Release information authorization (default: true)"
                        },
                        "AssignBen": {
                            "type": "boolean",
                            "description": "Optional: Assign benefits authorization"
                        },
                        "SubscNote": {
                            "type": "string",
                            "description": "Optional: Subscriber note"
                        }
                    },
                    "required": ["PlanNum", "Subscriber", "SubscriberID"]
                }
            },
            {
                "name": "update_insurance_subscription",
                "description": "Update an existing insurance subscription. WARNING: This will modify insurance subscription data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ins_sub_id": {
                            "type": "string",
                            "description": "Insurance subscription ID (InsSubNum) to update - required"
                        },
                        "PlanNum": {
                            "type": "string",
                            "description": "Insurance plan ID (PlanNum)"
                        },
                        "Subscriber": {
                            "type": "string",
                            "description": "Subscriber patient ID (PatNum)"
                        },
                        "SubscriberID": {
                            "type": "string",
                            "description": "Subscriber ID"
                        },
                        "DateEffective": {
                            "type": "string",
                            "description": "Date plan became effective (YYYY-MM-DD)"
                        },
                        "DateTerm": {
                            "type": "string",
                            "description": "Date plan was terminated (YYYY-MM-DD)"
                        },
                        "BenefitNotes": {
                            "type": "string",
                            "description": "Benefit notes (overwrites existing)"
                        },
                        "ReleaseInfo": {
                            "type": "boolean",
                            "description": "Release information authorization"
                        },
                        "AssignBen": {
                            "type": "boolean",
                            "description": "Assign benefits authorization"
                        },
                        "SubscNote": {
                            "type": "string",
                            "description": "Subscriber note"
                        }
                    },
                    "required": ["ins_sub_id"]
                }
            },
            {
                "name": "delete_insurance_subscription",
                "description": "Delete an insurance subscription. WARNING: This will permanently delete the insurance subscription from Open Dental. Cannot delete if PatPlans are attached.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ins_sub_id": {
                            "type": "string",
                            "description": "Insurance subscription ID (InsSubNum) to delete - required"
                        }
                    },
                    "required": ["ins_sub_id"]
                }
            },
            {
                "name": "get_insurance_verification",
                "description": "Get a single insurance verification by ID (InsVerifyNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ins_verify_id": {
                            "type": "string",
                            "description": "Insurance verification ID (InsVerifyNum) - required"
                        }
                    },
                    "required": ["ins_verify_id"]
                }
            },
            {
                "name": "get_insurance_verifications",
                "description": "Get insurance verifications by criteria (verify type, FKey, date edited)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "verify_type": {
                            "type": "string",
                            "description": "Optional: Verification type ('PatientEnrollment' or 'InsuranceBenefit')"
                        },
                        "f_key": {
                            "type": "string",
                            "description": "Optional: Foreign key (PatPlanNum for PatientEnrollment, PlanNum for InsuranceBenefit)"
                        },
                        "sec_date_t_edit": {
                            "type": "string",
                            "description": "Optional: Last edited date (YYYY-MM-DD HH:MM:SS). Returns verifications on or after this date"
                        }
                    }
                }
            },
            {
                "name": "update_insurance_verification",
                "description": "Update an insurance verification. WARNING: This will modify insurance verification data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "DateLastVerified": {
                            "type": "string",
                            "description": "Optional: Date last verified (YYYY-MM-DD)"
                        },
                        "VerifyType": {
                            "type": "string",
                            "description": "Verification type ('PatientEnrollment' or 'InsuranceBenefit') - required"
                        },
                        "FKey": {
                            "type": "string",
                            "description": "Foreign key (PatPlanNum for PatientEnrollment, PlanNum for InsuranceBenefit) - required"
                        },
                        "DefNum": {
                            "type": "string",
                            "description": "Optional: Definition ID (DefNum where Category=38)"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Status note"
                        }
                    },
                    "required": ["VerifyType", "FKey"]
                }
            },
            {
                "name": "get_payments",
                "description": "Get payments by criteria (pay type, patient, date entry)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pay_type": {
                            "type": "string",
                            "description": "Optional: Payment type ID (DefNum where Category=10)"
                        },
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "date_entry": {
                            "type": "string",
                            "description": "Optional: Date entry (YYYY-MM-DD). Returns payments on or after this date"
                        }
                    }
                }
            },
            {
                "name": "create_payment",
                "description": "Create a new payment. WARNING: This will create a new payment in Open Dental. Payments apply to outstanding charges in FIFO order.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PayAmt": {
                            "type": "string",
                            "description": "Payment amount - required"
                        },
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "PayType": {
                            "type": "string",
                            "description": "Optional: Payment type ID (DefNum where Category=10)"
                        },
                        "PayDate": {
                            "type": "string",
                            "description": "Optional: Payment date (YYYY-MM-DD). Defaults to today"
                        },
                        "CheckNum": {
                            "type": "string",
                            "description": "Optional: Check number"
                        },
                        "PayNote": {
                            "type": "string",
                            "description": "Optional: Payment note"
                        },
                        "BankBranch": {
                            "type": "string",
                            "description": "Optional: Bank branch code"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (ClinicNum)"
                        },
                        "isPatientPreferred": {
                            "type": "boolean",
                            "description": "Optional: Apply to patient instead of family members (default: false)"
                        },
                        "isPrepayment": {
                            "type": "boolean",
                            "description": "Optional: Create as prepayment (default: false)"
                        },
                        "procNums": {
                            "type": "array",
                            "description": "Optional: Array of procedure IDs to apply payment to"
                        },
                        "payPlanNum": {
                            "type": "string",
                            "description": "Optional: Payment plan ID (PayPlanNum) for prepayment"
                        }
                    },
                    "required": ["PayAmt", "PatNum"]
                }
            },
            {
                "name": "create_payment_refund",
                "description": "Create a refund payment. WARNING: This will create a refund payment in Open Dental. Cannot refund payments attached to payment plans or with negative paysplits.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PayNum": {
                            "type": "string",
                            "description": "Payment ID (PayNum) to refund - required"
                        }
                    },
                    "required": ["PayNum"]
                }
            },
            {
                "name": "update_payment",
                "description": "Update an existing payment. WARNING: This will modify payment data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pay_num": {
                            "type": "string",
                            "description": "Payment ID (PayNum) to update - required"
                        },
                        "PayType": {
                            "type": "string",
                            "description": "Payment type ID (DefNum where Category=10)"
                        },
                        "CheckNum": {
                            "type": "string",
                            "description": "Check number"
                        },
                        "BankBranch": {
                            "type": "string",
                            "description": "Bank-branch code for checks"
                        },
                        "PayNote": {
                            "type": "string",
                            "description": "Note on payment"
                        },
                        "ProcessStatus": {
                            "type": "string",
                            "description": "Process status ('OnlineProcessed' or 'OnlinePending')"
                        }
                    },
                    "required": ["pay_num"]
                }
            },
            {
                "name": "update_payment_partial",
                "description": "Update payment allocation to specific procedures and/or payplan charges. WARNING: This will delete existing paysplits and create new ones. Rarely used.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pay_num": {
                            "type": "string",
                            "description": "Payment ID (PayNum) to update - required"
                        },
                        "procNumsAndAmounts": {
                            "type": "array",
                            "description": "Optional: Array of {ProcNum, Amount} pairs"
                        },
                        "payPlanChargeNumsAndAmounts": {
                            "type": "array",
                            "description": "Optional: Array of {PayPlanChargeNum, Amount} pairs"
                        }
                    },
                    "required": ["pay_num"]
                }
            },
            {
                "name": "get_pay_splits",
                "description": "Get pay splits by criteria (patient, payment, procedure)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "pay_num": {
                            "type": "string",
                            "description": "Optional: Payment ID (PayNum)"
                        },
                        "proc_num": {
                            "type": "string",
                            "description": "Optional: Procedure ID (ProcNum)"
                        }
                    }
                }
            },
            {
                "name": "update_pay_split",
                "description": "Update an existing pay split. WARNING: This will modify pay split data in Open Dental. Rarely used.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "split_num": {
                            "type": "string",
                            "description": "Pay split ID (SplitNum) to update - required"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Optional: Provider ID (ProvNum)"
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (ClinicNum)"
                        }
                    },
                    "required": ["split_num"]
                }
            },
            {
                "name": "get_payment_plan",
                "description": "Get a single payment plan by ID (PayPlanNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pay_plan_num": {
                            "type": "string",
                            "description": "Payment plan ID (PayPlanNum) - required"
                        }
                    },
                    "required": ["pay_plan_num"]
                }
            },
            {
                "name": "get_payment_plans",
                "description": "Get payment plans by patient or guarantor",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum). Either patient_id or guarantor is required"
                        },
                        "guarantor": {
                            "type": "string",
                            "description": "Optional: Guarantor ID (PatNum). Either patient_id or guarantor is required"
                        }
                    }
                }
            },
            {
                "name": "create_payment_plan_dynamic",
                "description": "Create a new dynamic payment plan. WARNING: This will create a new payment plan in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "PayAmt": {
                            "type": "string",
                            "description": "Optional: Amount due per payment. Either PayAmt or NumberOfPayments is required"
                        },
                        "NumberOfPayments": {
                            "type": "integer",
                            "description": "Optional: Total number of payments. Either PayAmt or NumberOfPayments is required"
                        },
                        "procNums": {
                            "type": "array",
                            "description": "Optional: Array of procedure IDs. Either procNums or adjNums is required"
                        },
                        "adjNums": {
                            "type": "array",
                            "description": "Optional: Array of adjustment IDs. Either procNums or adjNums is required"
                        },
                        "Guarantor": {
                            "type": "string",
                            "description": "Optional: Guarantor ID (PatNum). Defaults to patient"
                        },
                        "PayPlanDate": {
                            "type": "string",
                            "description": "Optional: Plan agreement date (YYYY-MM-DD). Defaults to today"
                        },
                        "APR": {
                            "type": "number",
                            "description": "Optional: Annual percentage rate. Default 0"
                        },
                        "DownPayment": {
                            "type": "number",
                            "description": "Optional: Down payment amount. Default 0.00"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Plan note"
                        },
                        "PlanCategory": {
                            "type": "string",
                            "description": "Optional: Plan category ID (DefNum where Category=47)"
                        },
                        "ChargeFrequency": {
                            "type": "string",
                            "description": "Optional: Charge frequency ('Weekly', 'EveryOtherWeek', 'Monthly', 'Quarterly', 'OrdinalWeekday'). Default 'Monthly'"
                        },
                        "DatePayPlanStart": {
                            "type": "string",
                            "description": "Optional: First payment due date (YYYY-MM-DD). Default one month after PayPlanDate"
                        },
                        "DateInterestStart": {
                            "type": "string",
                            "description": "Optional: Date interest can start (YYYY-MM-DD). Default minval"
                        },
                        "IsLocked": {
                            "type": "boolean",
                            "description": "Optional: Is locked. Default true. Required true if APR > 0"
                        },
                        "DynamicPayPlanTPOption": {
                            "type": "string",
                            "description": "Optional: Treatment plan option ('AwaitComplete' or 'TreatAsComplete'). Default 'AwaitComplete'"
                        }
                    },
                    "required": ["PatNum"]
                }
            },
            {
                "name": "get_treatment_plans",
                "description": "Get treatment plans by criteria (patient, status, date edited)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Optional: Patient ID (PatNum)"
                        },
                        "tp_status": {
                            "type": "string",
                            "description": "Optional: Treatment plan status ('Saved', 'Active', or 'Inactive')"
                        },
                        "sec_date_t_edit": {
                            "type": "string",
                            "description": "Optional: Last edited date (YYYY-MM-DD HH:MM:SS). Returns plans on or after this date"
                        }
                    }
                }
            },
            {
                "name": "create_treatment_plan",
                "description": "Create a new inactive treatment plan. WARNING: This will create a new treatment plan in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "Heading": {
                            "type": "string",
                            "description": "Optional: Plan heading. Default 'Inactive Treatment Plan'"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Plan note. Defaults to TreatmentPlanNote preference"
                        },
                        "TPType": {
                            "type": "string",
                            "description": "Optional: Plan type ('Insurance' or 'Discount'). Default 'Insurance'"
                        }
                    },
                    "required": ["PatNum"]
                }
            },
            {
                "name": "create_treatment_plan_saved",
                "description": "Create a saved treatment plan from an existing Active or Inactive plan. WARNING: This will create a new saved treatment plan in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "TreatPlanNum": {
                            "type": "string",
                            "description": "Treatment plan ID (TreatPlanNum) to copy from - required"
                        },
                        "Heading": {
                            "type": "string",
                            "description": "Optional: Plan heading. Defaults to original plan heading"
                        },
                        "UserNumPresenter": {
                            "type": "string",
                            "description": "Optional: Presenter user ID (UserNum). Default 0"
                        }
                    },
                    "required": ["TreatPlanNum"]
                }
            },
            {
                "name": "update_treatment_plan",
                "description": "Update a treatment plan. Can be used to sign a saved treatment plan. WARNING: This will modify treatment plan data in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "treat_plan_num": {
                            "type": "string",
                            "description": "Treatment plan ID (TreatPlanNum) to update - required"
                        },
                        "DateTP": {
                            "type": "string",
                            "description": "Optional: Treatment plan date (YYYY-MM-DD). Can only be set if TPStatus is 'Saved'"
                        },
                        "Heading": {
                            "type": "string",
                            "description": "Optional: Plan heading"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Optional: Plan note (overwrites existing)"
                        },
                        "ResponsParty": {
                            "type": "string",
                            "description": "Optional: Responsible party patient ID (PatNum)"
                        },
                        "TPType": {
                            "type": "string",
                            "description": "Optional: Plan type ('Insurance' or 'Discount')"
                        },
                        "SignatureText": {
                            "type": "string",
                            "description": "Optional: Patient signature text (typed name)"
                        },
                        "SignaturePracticeText": {
                            "type": "string",
                            "description": "Optional: Practice signature text"
                        },
                        "isSigned": {
                            "type": "boolean",
                            "description": "Optional: Sign the treatment plan (set to true)"
                        },
                        "isSignedPractice": {
                            "type": "boolean",
                            "description": "Optional: Sign as practice (set to true)"
                        }
                    },
                    "required": ["treat_plan_num"]
                }
            },
            {
                "name": "get_treatment_plan_procedures",
                "description": "Get treatment plan procedures by treatment plan ID (TreatPlanNum)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "treat_plan_num": {
                            "type": "string",
                            "description": "Treatment plan ID (TreatPlanNum) - required"
                        }
                    },
                    "required": ["treat_plan_num"]
                }
            },
            {
                "name": "update_treatment_plan_procedure",
                "description": "Update a treatment plan procedure. WARNING: This will modify procedure data in Open Dental. Only works for unsigned treatment plans.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "proc_tp_num": {
                            "type": "string",
                            "description": "Procedure treatment plan ID (ProcTPNum) to update - required"
                        },
                        "Priority": {
                            "type": "string",
                            "description": "Optional: Priority ID (DefNum where Category=20)"
                        },
                        "ToothNumTP": {
                            "type": "string",
                            "description": "Optional: Tooth number"
                        },
                        "Surf": {
                            "type": "string",
                            "description": "Optional: Tooth surfaces or area"
                        },
                        "ProcCode": {
                            "type": "string",
                            "description": "Optional: Procedure code (display text)"
                        },
                        "Descript": {
                            "type": "string",
                            "description": "Optional: Procedure description"
                        },
                        "FeeAmt": {
                            "type": "string",
                            "description": "Optional: Fee amount"
                        },
                        "PriInsAmt": {
                            "type": "string",
                            "description": "Optional: Primary insurance amount"
                        },
                        "SecInsAmt": {
                            "type": "string",
                            "description": "Optional: Secondary insurance amount"
                        },
                        "PatAmt": {
                            "type": "string",
                            "description": "Optional: Patient amount"
                        },
                        "Discount": {
                            "type": "string",
                            "description": "Optional: Discount amount"
                        },
                        "Prognosis": {
                            "type": "string",
                            "description": "Optional: Prognosis text"
                        },
                        "Dx": {
                            "type": "string",
                            "description": "Optional: Diagnosis text"
                        },
                        "ProcAbbr": {
                            "type": "string",
                            "description": "Optional: Procedure code abbreviation"
                        },
                        "FeeAllowed": {
                            "type": "string",
                            "description": "Optional: Fee allowed by primary insurance"
                        }
                    },
                    "required": ["proc_tp_num"]
                }
            },
            {
                "name": "create_treatment_plan_procedure",
                "description": "Add a procedure directly to a specific treatment plan by TreatPlanNum. WARNING: This will create a new procedure entry on the treatment plan in Open Dental. Use this to build alternative treatment plans (e.g., Plan A vs Plan B) by adding procedures to different inactive plans. Surfaces should use standard abbreviations: M=Mesial, D=Distal, F=Facial, L=Lingual, O=Occlusal, I=Incisal, B=Buccal.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "TreatPlanNum": {
                            "type": "string",
                            "description": "Treatment plan ID (TreatPlanNum) to add the procedure to - required"
                        },
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "ProcCode": {
                            "type": "string",
                            "description": "Procedure code (e.g., 'D6010', 'D2393', 'D7951') - required"
                        },
                        "ToothNumTP": {
                            "type": "string",
                            "description": "Optional: Tooth number (1-32 for permanent, A-T for primary)"
                        },
                        "Surf": {
                            "type": "string",
                            "description": "Optional: Tooth surfaces (e.g., 'MFL', 'DO', 'MODBL'). Use standard abbreviations."
                        },
                        "Descript": {
                            "type": "string",
                            "description": "Optional: Procedure description. If not provided, uses the default description for the procedure code."
                        },
                        "FeeAmt": {
                            "type": "string",
                            "description": "Optional: Fee amount. If not set, uses fee schedule amount."
                        },
                        "PriInsAmt": {
                            "type": "string",
                            "description": "Optional: Primary insurance estimated amount"
                        },
                        "SecInsAmt": {
                            "type": "string",
                            "description": "Optional: Secondary insurance estimated amount"
                        },
                        "PatAmt": {
                            "type": "string",
                            "description": "Optional: Patient estimated amount"
                        },
                        "Discount": {
                            "type": "string",
                            "description": "Optional: Discount amount"
                        },
                        "Priority": {
                            "type": "string",
                            "description": "Optional: Priority ID (DefNum where Category=20)"
                        },
                        "Prognosis": {
                            "type": "string",
                            "description": "Optional: Prognosis text"
                        },
                        "Dx": {
                            "type": "string",
                            "description": "Optional: Diagnosis text"
                        },
                        "ProvNum": {
                            "type": "string",
                            "description": "Optional: Provider ID (ProvNum)"
                        },
                        "DateTP": {
                            "type": "string",
                            "description": "Optional: Treatment plan date (YYYY-MM-DD). Defaults to today."
                        },
                        "ClinicNum": {
                            "type": "string",
                            "description": "Optional: Clinic ID (ClinicNum)"
                        },
                        "ItemOrder": {
                            "type": "string",
                            "description": "Optional: Display order of this procedure within the treatment plan"
                        }
                    },
                    "required": ["TreatPlanNum", "PatNum", "ProcCode"]
                }
            },
            {
                "name": "delete_treatment_plan_procedure",
                "description": "Delete a treatment plan procedure. WARNING: This will permanently delete the procedure from Open Dental. Only works for unsigned treatment plans.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "proc_tp_num": {
                            "type": "string",
                            "description": "Procedure treatment plan ID (ProcTPNum) to delete - required"
                        }
                    },
                    "required": ["proc_tp_num"]
                }
            },
            {
                "name": "get_patient_info",
                "description": "Get patient information from Chart Module (age, billing type, insurance, medications, allergies, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_progress_notes",
                "description": "Get progress notes from Chart Module (appointments, procedures, commlogs, tasks, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Optional: Offset for pagination"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "get_planned_appointments",
                "description": "Get planned appointments from Chart Module",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            # ── Popup tools (direct DB) ──────────────────────────────
            {
                "name": "get_popups",
                "description": "Get popup alerts for a patient or matching a description filter. Requires direct database connection.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - optional, filters by patient"
                        },
                        "include_disabled": {
                            "type": "boolean",
                            "description": "Include disabled popups (default false)"
                        },
                        "description_contains": {
                            "type": "string",
                            "description": "Filter popups whose Description contains this text (case-insensitive LIKE)"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "create_popup",
                "description": "Create a single popup alert for a patient. Requires direct database connection.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "description": {
                            "type": "string",
                            "description": "Popup message text - required"
                        },
                        "popup_level": {
                            "type": "integer",
                            "description": "Popup level (default 0)"
                        }
                    },
                    "required": ["patient_id", "description"]
                }
            },
            {
                "name": "create_popups_batch",
                "description": "Create multiple popup alerts in a single call. Each popup is inserted individually so partial success is possible. Requires direct database connection.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "popups": {
                            "type": "array",
                            "description": "Array of popup objects to create",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "patient_id": {
                                        "type": "string",
                                        "description": "Patient ID (PatNum)"
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Popup message text"
                                    },
                                    "popup_level": {
                                        "type": "integer",
                                        "description": "Popup level (default 0)"
                                    }
                                },
                                "required": ["patient_id", "description"]
                            }
                        }
                    },
                    "required": ["popups"]
                }
            },
            {
                "name": "disable_popups",
                "description": "Disable popup alerts matching the given criteria. At least one filter (patient_id or description_contains) is required. Requires direct database connection.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - optional filter"
                        },
                        "description_contains": {
                            "type": "string",
                            "description": "Disable popups whose Description contains this text (case-insensitive LIKE)"
                        },
                        "disable_all_matching": {
                            "type": "boolean",
                            "description": "Must be true to confirm bulk disable when using description_contains without patient_id (safety guard, default false)"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "create_commlog",
                "description": "Create a new communication log entry for a patient. WARNING: This will create a new commlog in Open Dental.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "PatNum": {
                            "type": "string",
                            "description": "Patient ID (PatNum) - required"
                        },
                        "Note": {
                            "type": "string",
                            "description": "Communication note text - required"
                        },
                        "CommDateTime": {
                            "type": "string",
                            "description": "Optional: Date/time of communication (YYYY-MM-DD HH:MM:SS). Defaults to now"
                        },
                        "CommType": {
                            "type": "string",
                            "description": "Optional: Communication type (DefNum where Category=27)"
                        },
                        "commType": {
                            "type": "string",
                            "description": "Optional: Communication type by name (ItemName where Category=27). Takes precedence over CommType"
                        },
                        "Mode_": {
                            "type": "string",
                            "description": "Optional: Communication mode ('None', 'Email', 'Mail', 'Phone', 'In Person', 'Text', 'Email and Text', 'Phone and Text'). Default 'Phone'"
                        },
                        "SentOrReceived": {
                            "type": "string",
                            "description": "Optional: Direction ('Neither', 'Sent', 'Received'). Default 'Sent'"
                        }
                    },
                    "required": ["PatNum", "Note"]
                }
            }
        ]

    def call_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Call a tool by name with arguments"""
        try:
            if tool_name == "list_resources":
                return self._list_resources()
            elif tool_name == "get_patient":
                return self._get_patient(arguments.get("patient_id"))
            elif tool_name == "search_patients":
                return self._search_patients(arguments)
            elif tool_name == "get_appointment":
                return self._get_appointment(arguments.get("appointment_id"))
            elif tool_name == "search_appointments":
                return self._search_appointments(arguments)
            elif tool_name == "get_provider":
                return self._get_provider(arguments.get("provider_id"))
            elif tool_name == "list_providers":
                return self._list_providers()
            elif tool_name == "get_laboratory":
                return self._get_laboratory(arguments.get("laboratory_id"))
            elif tool_name == "list_laboratories":
                return self._list_laboratories()
            elif tool_name == "get_lab_cases":
                return self._get_lab_cases(arguments.get("patient_id"))
            elif tool_name == "get_patient_documents":
                return self._get_patient_documents(arguments.get("patient_id"))
            elif tool_name == "get_document":
                return self._get_document(arguments.get("document_id"))
            elif tool_name == "get_procedure_codes":
                return self._get_procedure_codes(arguments.get("limit"))
            elif tool_name == "get_statistics":
                return self._get_statistics()
            elif tool_name == "get_accurate_count":
                return self._get_accurate_count(arguments.get("resource"), arguments.get("search_params", {}))
            elif tool_name == "get_patient_appointments":
                return self._get_patient_appointments(
                    arguments.get("patient_id"),
                    arguments.get("date_from"),
                    arguments.get("date_to"),
                )
            elif tool_name == "get_todays_appointments":
                return self._get_todays_appointments(arguments.get("provider_id"))
            elif tool_name == "get_upcoming_appointments":
                return self._get_upcoming_appointments(arguments.get("days_ahead", 7), arguments.get("provider_id"))
            elif tool_name == "get_patient_by_phone":
                return self._get_patient_by_phone(arguments.get("phone"))
            elif tool_name == "get_patient_by_email":
                return self._get_patient_by_email(arguments.get("email"))
            elif tool_name == "get_appointments_by_date_range":
                return self._get_appointments_by_date_range(arguments.get("start_date"), arguments.get("end_date"), arguments.get("provider_id"), arguments.get("status"))
            elif tool_name == "search_patients_by_name":
                return self._search_patients_by_name(arguments.get("first_name"), arguments.get("last_name"))
            elif tool_name == "get_provider_appointments":
                return self._get_provider_appointments(arguments.get("provider_id"), arguments.get("date_from"), arguments.get("date_to"))
            elif tool_name == "get_lab_case":
                return self._get_lab_case(arguments.get("lab_case_id"))
            elif tool_name == "get_procedure_code":
                return self._get_procedure_code(arguments.get("procedure_code"))
            elif tool_name == "search_procedure_codes":
                return self._search_procedure_codes(arguments.get("search_term"))
            elif tool_name == "get_patient_summary":
                return self._get_patient_summary(arguments.get("patient_id"))
            elif tool_name == "get_appointment_summary":
                return self._get_appointment_summary(arguments.get("appointment_id"))
            elif tool_name == "get_provider_schedule":
                return self._get_provider_schedule(arguments.get("provider_id"), arguments.get("date"), arguments.get("days", 1))
            elif tool_name == "get_practice_overview":
                return self._get_practice_overview()
            elif tool_name == "create_patient":
                return self._create_patient(arguments)
            elif tool_name == "update_patient":
                return self._update_patient(arguments.get("patient_id"), arguments)
            elif tool_name == "create_appointment":
                return self._create_appointment(arguments)
            elif tool_name == "update_appointment":
                return self._update_appointment(arguments.get("appointment_id"), arguments)
            elif tool_name == "create_lab_case":
                return self._create_lab_case(arguments)
            elif tool_name == "update_lab_case":
                return self._update_lab_case(arguments.get("lab_case_id"), arguments)
            elif tool_name == "update_procedure_log":
                return self._update_procedure_log(arguments.get("procedure_id"), arguments)
            elif tool_name == "create_procedure_log":
                return self._create_procedure_log(arguments)
            elif tool_name == "create_document":
                return self._create_document(arguments)
            elif tool_name == "create_procnote":
                return self._create_procnote(
                    arguments.get("proc_num"),
                    arguments.get("pat_num"),
                    arguments.get("note"),
                    arguments.get("user_num", 0),
                )
            elif tool_name == "upload_document":
                return self._upload_document(arguments)
            elif tool_name == "query_database":
                return self._query_database(arguments.get("query"), arguments.get("limit", 1000))
            elif tool_name == "smart_query":
                return self._smart_query(
                    description=arguments.get("description"),
                    max_iterations=arguments.get("max_iterations"),
                    read_only=arguments.get("read_only"),
                    validate_results=arguments.get("validate_results", True),
                    schema_hints=arguments.get("schema_hints")
                )
            elif tool_name == "get_patient_aging":
                return self._get_patient_aging(arguments.get("patient_id"))
            elif tool_name == "get_patient_balances":
                return self._get_patient_balances(arguments.get("patient_id"))
            elif tool_name == "get_patient_service_date_view":
                return self._get_patient_service_date_view(arguments.get("patient_id"), arguments.get("is_family", False))
            elif tool_name == "get_adjustments":
                return self._get_adjustments(arguments.get("patient_id"), arguments.get("adj_type"), arguments.get("proc_num"))
            elif tool_name == "create_adjustment":
                return self._create_adjustment(arguments)
            elif tool_name == "update_adjustment":
                return self._update_adjustment(arguments.get("adjustment_id"), arguments)
            elif tool_name == "get_allergies":
                return self._get_allergies(arguments.get("patient_id"))
            elif tool_name == "create_allergy":
                return self._create_allergy(arguments)
            elif tool_name == "update_allergy":
                return self._update_allergy(arguments.get("allergy_id"), arguments)
            elif tool_name == "delete_allergy":
                return self._delete_allergy(arguments.get("allergy_id"))
            elif tool_name == "get_procedure_log":
                return self._get_procedure_log(arguments.get("procedure_id"))
            elif tool_name == "get_procedure_logs":
                return self._get_procedure_logs(arguments.get("patient_id"), arguments.get("appointment_id"), arguments.get("proc_status"))
            elif tool_name == "get_claim":
                return self._get_claim(arguments.get("claim_id"))
            elif tool_name == "get_claims":
                return self._get_claims(arguments.get("patient_id"), arguments.get("claim_status"), arguments.get("sec_date_t_edit"))
            elif tool_name == "create_claim":
                return self._create_claim(arguments)
            elif tool_name == "update_claim":
                return self._update_claim(arguments.get("claim_id"), arguments)
            elif tool_name == "get_claim_proc":
                return self._get_claim_proc(arguments.get("claim_proc_id"))
            elif tool_name == "get_claim_procs":
                return self._get_claim_procs(arguments.get("proc_num"), arguments.get("claim_num"), arguments.get("patient_id"), arguments.get("status"), arguments.get("claim_payment_num"))
            elif tool_name == "update_claim_proc":
                return self._update_claim_proc(arguments.get("claim_proc_id"), arguments)
            elif tool_name == "get_claim_payment":
                return self._get_claim_payment(arguments.get("claim_payment_id"))
            elif tool_name == "get_claim_payments":
                return self._get_claim_payments(arguments.get("sec_date_t_edit"))
            elif tool_name == "create_claim_payment":
                return self._create_claim_payment(arguments)
            elif tool_name == "create_claim_payment_batch":
                return self._create_claim_payment_batch(arguments)
            elif tool_name == "update_claim_payment":
                return self._update_claim_payment(arguments.get("claim_payment_id"), arguments)
            elif tool_name == "delete_claim_payment":
                return self._delete_claim_payment(arguments.get("claim_payment_id"))
            elif tool_name == "get_insurance_plan":
                return self._get_insurance_plan(arguments.get("plan_id"))
            elif tool_name == "get_insurance_plans":
                return self._get_insurance_plans(arguments.get("plan_type"), arguments.get("carrier_num"))
            elif tool_name == "create_insurance_plan":
                return self._create_insurance_plan(arguments)
            elif tool_name == "update_insurance_plan":
                return self._update_insurance_plan(arguments.get("plan_id"), arguments)
            elif tool_name == "get_insurance_subscription":
                return self._get_insurance_subscription(arguments.get("ins_sub_id"))
            elif tool_name == "get_insurance_subscriptions":
                return self._get_insurance_subscriptions(arguments.get("plan_num"), arguments.get("subscriber"), arguments.get("sec_date_t_edit"))
            elif tool_name == "create_insurance_subscription":
                return self._create_insurance_subscription(arguments)
            elif tool_name == "update_insurance_subscription":
                return self._update_insurance_subscription(arguments.get("ins_sub_id"), arguments)
            elif tool_name == "delete_insurance_subscription":
                return self._delete_insurance_subscription(arguments.get("ins_sub_id"))
            elif tool_name == "get_insurance_verification":
                return self._get_insurance_verification(arguments.get("ins_verify_id"))
            elif tool_name == "get_insurance_verifications":
                return self._get_insurance_verifications(arguments.get("verify_type"), arguments.get("f_key"), arguments.get("sec_date_t_edit"))
            elif tool_name == "update_insurance_verification":
                return self._update_insurance_verification(arguments)
            elif tool_name == "get_payments":
                return self._get_payments(arguments.get("pay_type"), arguments.get("patient_id"), arguments.get("date_entry"))
            elif tool_name == "create_payment":
                return self._create_payment(arguments)
            elif tool_name == "create_payment_refund":
                return self._create_payment_refund(arguments.get("PayNum"))
            elif tool_name == "update_payment":
                return self._update_payment(arguments.get("pay_num"), arguments)
            elif tool_name == "update_payment_partial":
                return self._update_payment_partial(arguments.get("pay_num"), arguments)
            elif tool_name == "get_pay_splits":
                return self._get_pay_splits(arguments.get("patient_id"), arguments.get("pay_num"), arguments.get("proc_num"))
            elif tool_name == "update_pay_split":
                return self._update_pay_split(arguments.get("split_num"), arguments)
            elif tool_name == "get_payment_plan":
                return self._get_payment_plan(arguments.get("pay_plan_num"))
            elif tool_name == "get_payment_plans":
                return self._get_payment_plans(arguments.get("patient_id"), arguments.get("guarantor"))
            elif tool_name == "create_payment_plan_dynamic":
                return self._create_payment_plan_dynamic(arguments)
            elif tool_name == "get_treatment_plans":
                return self._get_treatment_plans(arguments.get("patient_id"), arguments.get("tp_status"), arguments.get("sec_date_t_edit"))
            elif tool_name == "create_treatment_plan":
                return self._create_treatment_plan(arguments)
            elif tool_name == "create_treatment_plan_saved":
                return self._create_treatment_plan_saved(arguments)
            elif tool_name == "update_treatment_plan":
                return self._update_treatment_plan(arguments.get("treat_plan_num"), arguments)
            elif tool_name == "get_treatment_plan_procedures":
                return self._get_treatment_plan_procedures(arguments.get("treat_plan_num"))
            elif tool_name == "update_treatment_plan_procedure":
                return self._update_treatment_plan_procedure(arguments.get("proc_tp_num"), arguments)
            elif tool_name == "delete_treatment_plan_procedure":
                return self._delete_treatment_plan_procedure(arguments.get("proc_tp_num"))
            elif tool_name == "create_treatment_plan_procedure":
                return self._create_treatment_plan_procedure(arguments)
            elif tool_name == "get_patient_info":
                return self._get_patient_info(arguments.get("patient_id"))
            elif tool_name == "get_progress_notes":
                return self._get_progress_notes(arguments.get("patient_id"), arguments.get("offset"))
            elif tool_name == "get_planned_appointments":
                return self._get_planned_appointments(arguments.get("patient_id"))
            # ── Popup tools ──
            elif tool_name == "get_popups":
                return self._get_popups(
                    patient_id=arguments.get("patient_id") or arguments.get("PatNum"),
                    include_disabled=arguments.get("include_disabled", False),
                    description_contains=arguments.get("description_contains")
                )
            elif tool_name == "create_popup":
                return self._create_popup(arguments)
            elif tool_name == "create_popups_batch":
                return self._create_popups_batch(arguments.get("popups", []))
            elif tool_name == "disable_popups":
                return self._disable_popups(
                    patient_id=arguments.get("patient_id") or arguments.get("PatNum"),
                    description_contains=arguments.get("description_contains"),
                    disable_all_matching=arguments.get("disable_all_matching", False)
                )
            # ── Commlog tools ──
            elif tool_name == "create_commlog":
                return self._create_commlog(arguments)
            else:
                raise ValueError(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
            raise
    
    def _list_resources(self) -> Dict:
        """List all available Open Dental API resources"""
        return {
            "resources": [
                {
                    "name": "patients",
                    "endpoint": "/patients",
                    "description": "Patient records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "appointments",
                    "endpoint": "/appointments",
                    "description": "Appointment records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "providers",
                    "endpoint": "/providers",
                    "description": "Provider records",
                    "methods": ["GET"]
                },
                {
                    "name": "laboratories",
                    "endpoint": "/laboratories",
                    "description": "Laboratory records",
                    "methods": ["GET"]
                },
                {
                    "name": "labcases",
                    "endpoint": "/labcases",
                    "description": "Lab case records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "documents",
                    "endpoint": "/documents",
                    "description": "Document records",
                    "methods": ["GET", "POST"]
                },
                {
                    "name": "procedurecodes",
                    "endpoint": "/procedurecodes",
                    "description": "Procedure code definitions",
                    "methods": ["GET"]
                },
                {
                    "name": "procedurelogs",
                    "endpoint": "/procedurelogs",
                    "description": "Procedure log records (actual procedures)",
                    "methods": ["GET", "PUT"]
                },
                {
                    "name": "accountmodules",
                    "endpoint": "/accountmodules",
                    "description": "Account module endpoints (Aging, PatientBalances, ServiceDateView)",
                    "methods": ["GET"]
                },
                {
                    "name": "adjustments",
                    "endpoint": "/adjustments",
                    "description": "Adjustment records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "allergies",
                    "endpoint": "/allergies",
                    "description": "Allergy records",
                    "methods": ["GET", "POST", "PUT", "DELETE"]
                },
                {
                    "name": "claims",
                    "endpoint": "/claims",
                    "description": "Claim records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "claimprocs",
                    "endpoint": "/claimprocs",
                    "description": "Claim procedure records",
                    "methods": ["GET", "PUT"]
                },
                {
                    "name": "claimpayments",
                    "endpoint": "/claimpayments",
                    "description": "Claim payment records",
                    "methods": ["GET", "POST", "PUT", "DELETE"]
                },
                {
                    "name": "insplans",
                    "endpoint": "/insplans",
                    "description": "Insurance plan records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "inssubs",
                    "endpoint": "/inssubs",
                    "description": "Insurance subscription records",
                    "methods": ["GET", "POST", "PUT", "DELETE"]
                },
                {
                    "name": "insverifies",
                    "endpoint": "/insverifies",
                    "description": "Insurance verification records",
                    "methods": ["GET", "PUT"]
                },
                {
                    "name": "payments",
                    "endpoint": "/payments",
                    "description": "Payment records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "paysplits",
                    "endpoint": "/paysplits",
                    "description": "Pay split records",
                    "methods": ["GET", "PUT"]
                },
                {
                    "name": "payplans",
                    "endpoint": "/payplans",
                    "description": "Payment plan records",
                    "methods": ["GET", "POST"]
                },
                {
                    "name": "treatplans",
                    "endpoint": "/treatplans",
                    "description": "Treatment plan records",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "proctps",
                    "endpoint": "/proctps",
                    "description": "Treatment plan procedure records",
                    "methods": ["GET", "PUT", "DELETE"]
                },
                {
                    "name": "chartmodules",
                    "endpoint": "/chartmodules",
                    "description": "Chart module endpoints (PatientInfo, ProgNotes, PlannedAppts)",
                    "methods": ["GET"]
                },
                {
                    "name": "popups",
                    "endpoint": "direct_db_only",
                    "description": "Patient popup alerts (direct database access only - no REST API)",
                    "methods": ["GET", "POST", "PUT"]
                },
                {
                    "name": "commlogs",
                    "endpoint": "/commlogs",
                    "description": "Communication log records",
                    "methods": ["GET", "POST", "PUT"]
                }
            ],
            "api_url": self.api_url
        }
    
    def _get_patient(self, patient_id: str) -> Dict:
        """Get a patient by ID"""
        return self._make_request("GET", f"/patients/{patient_id}")
    
    def _search_patients(self, params: Dict) -> Dict:
        """Search for patients"""
        query_params = {}
        if params.get("last_name"):
            query_params["LName"] = params["last_name"]
        if params.get("first_name"):
            query_params["FName"] = params["first_name"]
        if params.get("phone"):
            query_params["Phone"] = params["phone"]
        if params.get("email"):
            query_params["Email"] = params["email"]
        if params.get("birthdate"):
            query_params["Birthdate"] = params["birthdate"]
        if params.get("hide_inactive") is not None:
            query_params["hideInactive"] = params["hide_inactive"]
        
        result = self._make_request("GET", "/patients", params=query_params)
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "patients": result
        }
    
    def _get_appointment(self, appointment_id: str) -> Dict:
        """Get an appointment by ID"""
        return self._make_request("GET", f"/appointments/{appointment_id}")
    
    def _search_appointments(self, params: Dict) -> Dict:
        """Search for appointments"""
        query_params = {}
        if params.get("patient_id"):
            query_params["PatNum"] = params["patient_id"]
        if params.get("date_from"):
            query_params["dateStart"] = params["date_from"]
        if params.get("date_to"):
            query_params["dateEnd"] = params["date_to"]
        if params.get("status"):
            query_params["AptStatus"] = params["status"]

        all_appointments = []
        offset = 0

        # Open Dental API pages appointments in blocks of 100 records via Offset.
        while True:
            page_params = dict(query_params)
            page_params["Offset"] = offset
            page = self._make_request("GET", "/appointments", params=page_params)

            if not isinstance(page, list):
                return {
                    "count": 1,
                    "appointments": page
                }

            all_appointments.extend(page)
            if len(page) < 100:
                break
            offset += 100

        return {
            "count": len(all_appointments),
            "appointments": all_appointments
        }
    
    def _get_provider(self, provider_id: str) -> Dict:
        """Get a provider by ID"""
        return self._make_request("GET", f"/providers/{provider_id}")
    
    def _list_providers(self) -> Dict:
        """List all providers"""
        result = self._make_request("GET", "/providers")
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "providers": result
        }
    
    def _get_laboratory(self, laboratory_id: str) -> Dict:
        """Get a laboratory by ID"""
        return self._make_request("GET", f"/laboratories/{laboratory_id}")
    
    def _list_laboratories(self) -> Dict:
        """List all laboratories"""
        result = self._make_request("GET", "/laboratories")
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "laboratories": result
        }
    
    def _get_lab_cases(self, patient_id: str) -> Dict:
        """Get lab cases for a patient"""
        result = self._make_request("GET", "/labcases", params={"PatNum": patient_id})
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "lab_cases": result
        }
    
    def _get_patient_documents(self, patient_id: str) -> Dict:
        """Get documents for a patient"""
        result = self._make_request("GET", "/documents", params={"PatNum": patient_id})
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "documents": result
        }
    
    def _get_document(self, document_id: str) -> Dict:
        """Get a document by ID"""
        return self._make_request("GET", f"/documents/{document_id}")
    
    def _get_procedure_codes(self, limit: Optional[int] = None) -> Dict:
        """Get procedure codes"""
        params = {}
        if limit:
            params["limit"] = limit
        
        result = self._make_request("GET", "/procedurecodes", params=params)
        return {
            "count": len(result) if isinstance(result, list) else 1,
            "procedure_codes": result
        }
    
    def _get_statistics(self) -> Dict:
        """Get practice statistics with pagination support"""
        stats = {}
        
        try:
            # Get patient count - try to get all with pagination
            stats["total_patients"] = self._count_with_pagination("/patients", "patients")
            
            # Get appointment count - try to get all with pagination
            stats["total_appointments"] = self._count_with_pagination("/appointments", "appointments")
            
            # Get provider count (usually small, no pagination needed)
            providers = self._make_request("GET", "/providers")
            stats["total_providers"] = len(providers) if isinstance(providers, list) else 1
            
            # Get laboratory count (usually small, no pagination needed)
            laboratories = self._make_request("GET", "/laboratories")
            stats["total_laboratories"] = len(laboratories) if isinstance(laboratories, list) else 1
            
            # Add note about pagination
            if stats.get("total_patients") == 1000 or stats.get("total_appointments") == 1000:
                stats["note"] = "API returns maximum 1000 results per request. Actual counts may be higher. Use search filters to access more records."
            
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            stats["error"] = str(e)
        
        return stats
    
    def _count_with_pagination(self, endpoint: str, resource_name: str) -> int:
        """Count resources - API returns max 1000 per request"""
        try:
            # API appears to have a hard limit of 1000 results per request
            # We'll get the first 1000 and indicate if there might be more
            result = self._make_request("GET", endpoint, params={})
            if isinstance(result, list):
                count = len(result)
                # If we got exactly 1000, there are likely more
                if count == 1000:
                    # Try to estimate by getting the last ID and checking if there are more
                    # This is a best-effort approach since the API doesn't support pagination
                    try:
                        if endpoint == "/patients" and len(result) > 0:
                            last_patnum = result[-1].get("PatNum")
                            # Try to get patients after this ID
                            # Note: This is a workaround - the API may not support this
                            logger.info(f"API returned exactly 1000 {resource_name}. There may be more records.")
                    except:
                        pass
                return count
            return 1
        except Exception as e:
            logger.warning(f"Error counting {resource_name}: {e}")
            # Fallback to simple count
            try:
                result = self._make_request("GET", endpoint, params={})
                return len(result) if isinstance(result, list) else 1
            except:
                return 0
    
    def _get_accurate_count(self, resource: str, search_params: Dict) -> Dict:
        """Get more accurate count using search parameters"""
        try:
            if resource == "patients":
                result = self._search_patients(search_params)
                count = result.get("count", 0)
                return {
                    "resource": "patients",
                    "count": count,
                    "search_params": search_params,
                    "note": "This count is based on the search parameters provided. API returns max 1000 results per request."
                }
            elif resource == "appointments":
                result = self._search_appointments(search_params)
                count = result.get("count", 0)
                return {
                    "resource": "appointments",
                    "count": count,
                    "search_params": search_params,
                    "note": "This count is based on the search parameters provided. API returns max 1000 results per request."
                }
            else:
                return {
                    "error": f"Unsupported resource: {resource}",
                    "supported_resources": ["patients", "appointments"]
                }
        except Exception as e:
            logger.error(f"Error getting accurate count: {e}")
            return {
                "error": str(e)
            }
    
    def _get_patient_appointments(self, patient_id: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
        """Get all appointments for a patient"""
        params = {"patient_id": patient_id}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._search_appointments(params)
    
    def _get_todays_appointments(self, provider_id: Optional[str] = None) -> Dict:
        """Get today's appointments"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        all_appointments = []
        offset = 0

        # Open Dental API pages appointments in blocks of 100 records via Offset.
        while True:
            page = self._make_request(
                "GET",
                "/appointments",
                params={
                    "date": today,
                    "Offset": offset,
                },
            )

            if not isinstance(page, list):
                # Preserve existing behavior shape if API returns non-list payload.
                return {
                    "count": 1,
                    "appointments": page
                }

            all_appointments.extend(page)
            if len(page) < 100:
                break
            offset += 100

        if provider_id:
            all_appointments = [
                apt for apt in all_appointments
                if str(apt.get("ProvNum", "")) == str(provider_id)
            ]

        return {
            "count": len(all_appointments),
            "appointments": all_appointments
        }
    
    def _get_upcoming_appointments(self, days_ahead: int = 7, provider_id: Optional[str] = None) -> Dict:
        """Get upcoming appointments"""
        from datetime import datetime, timedelta
        today = datetime.now()
        end_date = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")

        all_appointments = []
        offset = 0

        # Open Dental API pages appointments in blocks of 100 records via Offset.
        while True:
            page = self._make_request(
                "GET",
                "/appointments",
                params={
                    "dateStart": today_str,
                    "dateEnd": end_date,
                    "Offset": offset,
                },
            )

            if not isinstance(page, list):
                return {
                    "count": 1,
                    "appointments": page
                }

            all_appointments.extend(page)
            if len(page) < 100:
                break
            offset += 100

        if provider_id:
            all_appointments = [
                apt for apt in all_appointments
                if str(apt.get("ProvNum", "")) == str(provider_id)
            ]

        return {
            "count": len(all_appointments),
            "appointments": all_appointments
        }
    
    def _get_patient_by_phone(self, phone: str) -> Dict:
        """Find patient by phone number"""
        return self._search_patients({"phone": phone})
    
    def _get_patient_by_email(self, email: str) -> Dict:
        """Find patient by email"""
        return self._search_patients({"email": email})
    
    def _get_appointments_by_date_range(self, start_date: str, end_date: str, provider_id: Optional[str] = None, status: Optional[str] = None) -> Dict:
        """Get appointments by date range"""
        all_appointments = []
        offset = 0

        # Open Dental API pages appointments in blocks of 100 records via Offset.
        while True:
            page = self._make_request(
                "GET",
                "/appointments",
                params={
                    "dateStart": start_date,
                    "dateEnd": end_date,
                    "Offset": offset,
                },
            )

            if not isinstance(page, list):
                # Preserve existing behavior shape if API returns non-list payload.
                return {
                    "count": 1,
                    "appointments": page
                }

            all_appointments.extend(page)
            if len(page) < 100:
                break
            offset += 100

        if provider_id:
            all_appointments = [
                apt for apt in all_appointments
                if str(apt.get("ProvNum", "")) == str(provider_id)
            ]

        if status:
            all_appointments = [
                apt for apt in all_appointments
                if str(apt.get("AptStatus", "")).lower() == str(status).lower()
            ]

        return {
            "count": len(all_appointments),
            "appointments": all_appointments
        }
    
    def _search_patients_by_name(self, first_name: Optional[str] = None, last_name: Optional[str] = None) -> Dict:
        """Search patients by name"""
        params = {}
        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        return self._search_patients(params)
    
    def _get_provider_appointments(self, provider_id: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
        """Get provider's appointments"""
        params = {"provider_id": provider_id}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        # Note: API might filter by provider via ProvNum in appointments
        # We'll get all appointments and filter client-side if needed
        all_appointments = self._search_appointments(params)
        if isinstance(all_appointments.get("appointments"), list):
            filtered = [apt for apt in all_appointments["appointments"] if str(apt.get("ProvNum", "")) == str(provider_id)]
            return {
                "count": len(filtered),
                "appointments": filtered
            }
        return all_appointments
    
    def _get_lab_case(self, lab_case_id: str) -> Dict:
        """Get a lab case by ID"""
        return self._make_request("GET", f"/labcases/{lab_case_id}")
    
    def _get_procedure_code(self, procedure_code: str) -> Dict:
        """Get a specific procedure code"""
        try:
            all_codes = self._get_procedure_codes(None)
            codes = all_codes.get("procedure_codes", [])
            if isinstance(codes, list):
                for code in codes:
                    if code.get("ProcCode", "").upper() == procedure_code.upper():
                        return code
            return {"error": f"Procedure code {procedure_code} not found"}
        except Exception as e:
            return {"error": str(e)}
    
    def _search_procedure_codes(self, search_term: str) -> Dict:
        """Search procedure codes"""
        try:
            all_codes = self._get_procedure_codes(None)
            codes = all_codes.get("procedure_codes", [])
            if isinstance(codes, list):
                search_lower = search_term.lower()
                matches = []
                for code in codes:
                    code_str = code.get("ProcCode", "").upper()
                    desc = code.get("Descript", "").lower()
                    if search_lower in code_str.lower() or search_lower in desc:
                        matches.append(code)
                return {
                    "count": len(matches),
                    "procedure_codes": matches
                }
            return {"count": 0, "procedure_codes": []}
        except Exception as e:
            return {"error": str(e)}
    
    def _get_patient_summary(self, patient_id: str) -> Dict:
        """Get comprehensive patient summary"""
        try:
            patient = self._get_patient(patient_id)
            appointments = self._get_patient_appointments(patient_id)
            lab_cases = self._get_lab_cases(patient_id)
            documents = self._get_patient_documents(patient_id)
            
            return {
                "patient": patient,
                "appointments": appointments,
                "lab_cases": lab_cases,
                "documents": documents,
                "summary": {
                    "appointment_count": appointments.get("count", 0),
                    "lab_case_count": lab_cases.get("count", 0),
                    "document_count": documents.get("count", 0)
                }
            }
        except Exception as e:
            logger.error(f"Error getting patient summary: {e}")
            return {"error": str(e)}
    
    def _get_appointment_summary(self, appointment_id: str) -> Dict:
        """Get comprehensive appointment summary"""
        try:
            appointment = self._get_appointment(appointment_id)
            patient_id = appointment.get("PatNum")
            provider_id = appointment.get("ProvNum")
            
            summary = {
                "appointment": appointment
            }
            
            if patient_id:
                try:
                    summary["patient"] = self._get_patient(str(patient_id))
                except:
                    pass
            
            if provider_id:
                try:
                    summary["provider"] = self._get_provider(str(provider_id))
                except:
                    pass
            
            return summary
        except Exception as e:
            logger.error(f"Error getting appointment summary: {e}")
            return {"error": str(e)}
    
    def _get_provider_schedule(self, provider_id: str, date: Optional[str] = None, days: int = 1) -> Dict:
        """Get provider's schedule"""
        from datetime import datetime, timedelta
        
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        
        start_date = datetime.strptime(date, "%Y-%m-%d")
        end_date = (start_date + timedelta(days=days-1)).strftime("%Y-%m-%d")
        start_date_str = start_date.strftime("%Y-%m-%d")
        
        appointments = self._get_provider_appointments(provider_id, start_date_str, end_date)
        
        return {
            "provider_id": provider_id,
            "date": date,
            "days": days,
            "appointments": appointments
        }
    
    def _get_practice_overview(self) -> Dict:
        """Get practice overview"""
        from datetime import datetime
        
        try:
            stats = self._get_statistics()
            today = datetime.now().strftime("%Y-%m-%d")
            todays_appts = self._get_todays_appointments()

            # Use direct SQL counts to avoid REST API 1000-row cap in overview totals.
            def _safe_sql_count(query: str) -> Optional[int]:
                result = self._query_database(query, limit=1)
                if not result.get("success"):
                    return None
                rows = result.get("rows", [])
                if not rows:
                    return 0
                first_row = rows[0]
                if isinstance(first_row, dict):
                    for key in ("total", "count", "COUNT(*)"):
                        if key in first_row:
                            try:
                                return int(first_row[key])
                            except Exception:
                                pass
                    try:
                        return int(next(iter(first_row.values())))
                    except Exception:
                        return None
                return None

            total_patients_sql = _safe_sql_count("SELECT COUNT(*) AS total FROM patient")
            total_appointments_sql = _safe_sql_count("SELECT COUNT(*) AS total FROM appointment")
            
            overview = {
                "date": today,
                "statistics": stats,
                "todays_appointments": todays_appts,
                "summary": {
                    "total_patients": (
                        total_patients_sql
                        if total_patients_sql is not None
                        else stats.get("total_patients", 0)
                    ),
                    "total_appointments": (
                        total_appointments_sql
                        if total_appointments_sql is not None
                        else stats.get("total_appointments", 0)
                    ),
                    "total_providers": stats.get("total_providers", 0),
                    "total_laboratories": stats.get("total_laboratories", 0),
                    "todays_appointment_count": todays_appts.get("count", 0)
                }
            }
            
            return overview
        except Exception as e:
            logger.error(f"Error getting practice overview: {e}")
            return {"error": str(e)}
    
    def _create_patient(self, patient_data: Dict) -> Dict:
        """Create a new patient"""
        try:
            # Remove patient_id if present (not needed for create)
            patient_data = {k: v for k, v in patient_data.items() if k != "patient_id"}
            result = self._make_request("POST", "/patients", data=patient_data)
            return {
                "success": True,
                "message": "Patient created successfully",
                "patient": result
            }
        except Exception as e:
            logger.error(f"Error creating patient: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_patient(self, patient_id: str, patient_data: Dict) -> Dict:
        """Update an existing patient"""
        try:
            # Remove patient_id from data (it's in the URL)
            patient_data = {k: v for k, v in patient_data.items() if k != "patient_id"}
            result = self._make_request("PUT", f"/patients/{patient_id}", data=patient_data)
            return {
                "success": True,
                "message": "Patient updated successfully",
                "patient": result
            }
        except Exception as e:
            logger.error(f"Error updating patient: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_appointment(self, appointment_data: Dict) -> Dict:
        """Create a new appointment"""
        try:
            # Set default Pattern to 1 hour (8 slots) if not provided
            # Pattern format: "//XXXXXXXX//" (slashes around X's is Open Dental convention)
            if "Pattern" not in appointment_data:
                appointment_data["Pattern"] = "//XXXXXXXX//"  # 8 slots = 1 hour default
            
            # Ensure Pattern has proper format (slashes around X's)
            pattern = appointment_data.get("Pattern", "")
            if pattern and not pattern.startswith("/") and not pattern.endswith("/"):
                # Add slashes if not present (Open Dental convention)
                appointment_data["Pattern"] = f"//{pattern}//"
            
            # Check for existing appointment at same time to prevent duplicates
            apt_datetime = appointment_data.get("AptDateTime", "")
            prov_num = appointment_data.get("ProvNum", "")
            if apt_datetime and prov_num:
                try:
                    # Check if appointment already exists at this time for this provider
                    existing = self._make_request("GET", "/appointments", params={
                        "AptDateTime": apt_datetime,
                        "ProvNum": prov_num
                    })
                    if isinstance(existing, list) and len(existing) > 0:
                        # Check if any existing appointment is for the same patient
                        pat_num = appointment_data.get("PatNum", "")
                        for apt in existing:
                            if str(apt.get("PatNum", "")) == str(pat_num):
                                return {
                                    "success": False,
                                    "error": f"Appointment already exists for patient {pat_num} at {apt_datetime}",
                                    "existing_appointment": apt
                                }
                except Exception as check_error:
                    # If check fails, continue with creation (might be API limitation)
                    logger.warning(f"Could not check for existing appointments: {check_error}")
            
            result = self._make_request("POST", "/appointments", data=appointment_data)
            return {
                "success": True,
                "message": "Appointment created successfully",
                "appointment": result
            }
        except Exception as e:
            logger.error(f"Error creating appointment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_appointment(self, appointment_id: str, appointment_data: Dict) -> Dict:
        """Update an existing appointment"""
        try:
            # Remove appointment_id from data (it's in the URL)
            appointment_data = {k: v for k, v in appointment_data.items() if k != "appointment_id"}
            result = self._make_request("PUT", f"/appointments/{appointment_id}", data=appointment_data)
            return {
                "success": True,
                "message": "Appointment updated successfully",
                "appointment": result
            }
        except Exception as e:
            logger.error(f"Error updating appointment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_lab_case(self, lab_case_data: Dict) -> Dict:
        """Create a new lab case"""
        try:
            result = self._make_request("POST", "/labcases", data=lab_case_data)
            return {
                "success": True,
                "message": "Lab case created successfully",
                "lab_case": result
            }
        except Exception as e:
            logger.error(f"Error creating lab case: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_lab_case(self, lab_case_id: str, lab_case_data: Dict) -> Dict:
        """Update an existing lab case"""
        try:
            # Remove lab_case_id from data (it's in the URL)
            lab_case_data = {k: v for k, v in lab_case_data.items() if k != "lab_case_id"}
            result = self._make_request("PUT", f"/labcases/{lab_case_id}", data=lab_case_data)
            return {
                "success": True,
                "message": "Lab case updated successfully",
                "lab_case": result
            }
        except Exception as e:
            logger.error(f"Error updating lab case: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_procedure_log(self, procedure_id: str, procedure_data: Dict) -> Dict:
        """Update an existing procedure log (procedure code)"""
        try:
            # Remove procedure_id from data (it's in the URL)
            procedure_data = {k: v for k, v in procedure_data.items() if k != "procedure_id"}
            
            # Ensure at least one of procCode or CodeNum is provided
            if not procedure_data.get("procCode") and not procedure_data.get("CodeNum"):
                return {
                    "success": False,
                    "error": "Either procCode or CodeNum must be provided to update the procedure code"
                }
            
            # Only include procCode or CodeNum (not both) - API prefers procCode
            update_data = {}
            if procedure_data.get("procCode"):
                update_data["procCode"] = procedure_data["procCode"]
            elif procedure_data.get("CodeNum"):
                update_data["CodeNum"] = procedure_data["CodeNum"]
            
            result = self._make_request("PUT", f"/procedurelogs/{procedure_id}", data=update_data)
            return {
                "success": True,
                "message": "Procedure log updated successfully",
                "procedure": result
            }
        except Exception as e:
            logger.error(f"Error updating procedure log: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _create_procedure_log(self, procedure_data: Dict) -> Dict:
        """Create a new procedure log entry (treatment planned procedure).

        Creates a procedure via POST /procedurelogs. By default, procedures are
        created with ProcStatus 'TP' (Treatment Planned) and will appear on the
        patient's active treatment plan.

        Args:
            procedure_data: Dict containing at minimum PatNum and procCode.
                Optional fields: ToothNum, Surf, ProcDate, ProcStatus, ProvNum,
                ClinicNum, Dx, priority, ToothRange, ProcFee.

        Returns:
            Dict with success status and the created procedure data.
        """
        try:
            # Validate required fields
            if not procedure_data.get("PatNum"):
                return {
                    "success": False,
                    "error": "PatNum is required"
                }
            if not procedure_data.get("procCode"):
                return {
                    "success": False,
                    "error": "procCode is required (e.g., 'D6010', 'D2393')"
                }

            # Build the API payload with only recognized fields
            api_fields = [
                "PatNum", "procCode", "ToothNum", "Surf", "ProcDate",
                "ProcStatus", "ProvNum", "ClinicNum", "Dx", "priority",
                "ToothRange", "ProcFee", "CodeNum"
            ]
            payload = {k: v for k, v in procedure_data.items() if k in api_fields and v is not None and v != ""}

            # Default ProcStatus to TP (Treatment Planned) if not specified
            if "ProcStatus" not in payload:
                payload["ProcStatus"] = "TP"

            result = self._make_request("POST", "/procedurelogs", data=payload)
            return {
                "success": True,
                "message": f"Procedure log created successfully (procCode: {procedure_data.get('procCode')}, status: {payload.get('ProcStatus', 'TP')})",
                "procedure": result
            }
        except Exception as e:
            logger.error(f"Error creating procedure log: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _create_commlog(self, commlog_data: Dict) -> Dict:
        """Create a new communication log entry for a patient.

        Args:
            commlog_data: Dict containing at minimum PatNum and Note.
                Optional fields: CommDateTime, CommType, commType, Mode_, SentOrReceived.

        Returns:
            Dict with success status and the created commlog data.
        """
        try:
            if not commlog_data.get("PatNum"):
                return {"success": False, "error": "PatNum is required"}
            if not commlog_data.get("Note"):
                return {"success": False, "error": "Note is required"}

            api_fields = [
                "PatNum", "Note", "CommDateTime", "CommType",
                "commType", "Mode_", "SentOrReceived"
            ]
            payload = {k: v for k, v in commlog_data.items()
                       if k in api_fields and v is not None and v != ""}

            result = self._make_request("POST", "/commlogs", data=payload)
            return {
                "success": True,
                "message": f"Commlog created for patient {commlog_data['PatNum']}",
                "commlog": result
            }
        except Exception as e:
            logger.error(f"Error creating commlog: {e}")
            return {"success": False, "error": str(e)}

    def _get_patient_aging(self, patient_id: str) -> Dict:
        """Get aging information for a patient and their family"""
        try:
            result = self._make_request("GET", f"/accountmodules/{patient_id}/Aging")
            return {
                "success": True,
                "patient_id": patient_id,
                "aging": result
            }
        except Exception as e:
            logger.error(f"Error getting patient aging: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_patient_balances(self, patient_id: str) -> Dict:
        """Get patient portion balances for a patient's family"""
        try:
            result = self._make_request("GET", f"/accountmodules/{patient_id}/PatientBalances")
            return {
                "success": True,
                "patient_id": patient_id,
                "balances": result if isinstance(result, list) else [result]
            }
        except Exception as e:
            logger.error(f"Error getting patient balances: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_patient_service_date_view(self, patient_id: str, is_family: bool = False) -> Dict:
        """Get list of all charges and credits for a patient and their family"""
        try:
            params = {}
            if is_family:
                params["isFamily"] = "true"
            result = self._make_request("GET", f"/accountmodules/{patient_id}/ServiceDateView", params=params)
            return {
                "success": True,
                "patient_id": patient_id,
                "is_family": is_family,
                "service_date_view": result if isinstance(result, list) else [result]
            }
        except Exception as e:
            logger.error(f"Error getting service date view: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_adjustments(self, patient_id: str, adj_type: Optional[str] = None, proc_num: Optional[str] = None) -> Dict:
        """Get all adjustments for a patient"""
        try:
            params = {"PatNum": patient_id}
            if adj_type:
                params["AdjType"] = adj_type
            if proc_num:
                params["ProcNum"] = proc_num
            
            result = self._make_request("GET", "/adjustments", params=params)
            return {
                "success": True,
                "patient_id": patient_id,
                "adjustments": result if isinstance(result, list) else [result]
            }
        except Exception as e:
            logger.error(f"Error getting adjustments: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_adjustment(self, adjustment_data: Dict) -> Dict:
        """Create a new adjustment"""
        try:
            result = self._make_request("POST", "/adjustments", data=adjustment_data)
            return {
                "success": True,
                "message": "Adjustment created successfully",
                "adjustment": result
            }
        except Exception as e:
            logger.error(f"Error creating adjustment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_adjustment(self, adjustment_id: str, adjustment_data: Dict) -> Dict:
        """Update an existing adjustment"""
        try:
            # Remove adjustment_id from data (it's in the URL)
            adjustment_data = {k: v for k, v in adjustment_data.items() if k != "adjustment_id"}
            result = self._make_request("PUT", f"/adjustments/{adjustment_id}", data=adjustment_data)
            return {
                "success": True,
                "message": "Adjustment updated successfully",
                "adjustment": result
            }
        except Exception as e:
            logger.error(f"Error updating adjustment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_allergies(self, patient_id: str) -> Dict:
        """Get all allergies for a patient"""
        try:
            result = self._make_request("GET", "/allergies", params={"PatNum": patient_id})
            return {
                "success": True,
                "patient_id": patient_id,
                "allergies": result if isinstance(result, list) else [result]
            }
        except Exception as e:
            logger.error(f"Error getting allergies: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_allergy(self, allergy_data: Dict) -> Dict:
        """Create a new allergy record"""
        try:
            result = self._make_request("POST", "/allergies", data=allergy_data)
            return {
                "success": True,
                "message": "Allergy created successfully",
                "allergy": result
            }
        except Exception as e:
            logger.error(f"Error creating allergy: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_allergy(self, allergy_id: str, allergy_data: Dict) -> Dict:
        """Update an existing allergy record"""
        try:
            # Remove allergy_id from data (it's in the URL)
            allergy_data = {k: v for k, v in allergy_data.items() if k != "allergy_id"}
            result = self._make_request("PUT", f"/allergies/{allergy_id}", data=allergy_data)
            return {
                "success": True,
                "message": "Allergy updated successfully",
                "allergy": result
            }
        except Exception as e:
            logger.error(f"Error updating allergy: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _delete_allergy(self, allergy_id: str) -> Dict:
        """Delete an allergy record"""
        try:
            # DELETE method support
            url = f"{self.api_url}/allergies/{allergy_id}"
            response = self.session.delete(url)
            response.raise_for_status()
            if response.status_code == 204 or len(response.content) == 0:
                return {
                    "success": True,
                    "message": "Allergy deleted successfully"
                }
            return {
                "success": True,
                "message": "Allergy deleted successfully",
                "response": response.json() if response.content else None
            }
        except Exception as e:
            logger.error(f"Error deleting allergy: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_procedure_log(self, procedure_id: str) -> Dict:
        """Get a procedure log by ID"""
        try:
            result = self._make_request("GET", f"/procedurelogs/{procedure_id}")
            return {
                "success": True,
                "procedure": result
            }
        except Exception as e:
            logger.error(f"Error getting procedure log: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_procedure_logs(self, patient_id: Optional[str] = None, appointment_id: Optional[str] = None, proc_status: Optional[str] = None) -> Dict:
        """Get procedure logs by criteria"""
        try:
            params = {}
            if patient_id:
                params["PatNum"] = patient_id
            if appointment_id:
                params["AptNum"] = appointment_id
            if proc_status:
                params["ProcStatus"] = proc_status
            
            result = self._make_request("GET", "/procedurelogs", params=params)
            return {
                "success": True,
                "procedure_logs": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting procedure logs: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claim(self, claim_id: str) -> Dict:
        """Get a single claim by ID"""
        try:
            result = self._make_request("GET", f"/claims/{claim_id}")
            return {
                "success": True,
                "claim": result
            }
        except Exception as e:
            logger.error(f"Error getting claim: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claims(self, patient_id: Optional[str] = None, claim_status: Optional[str] = None, sec_date_t_edit: Optional[str] = None) -> Dict:
        """Get claims by criteria"""
        try:
            params = {}
            if patient_id:
                params["PatNum"] = patient_id
            if claim_status:
                params["ClaimStatus"] = claim_status
            if sec_date_t_edit:
                params["SecDateTEdit"] = sec_date_t_edit
            
            result = self._make_request("GET", "/claims", params=params)
            return {
                "success": True,
                "claims": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting claims: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_claim(self, claim_data: Dict) -> Dict:
        """Create a new claim"""
        try:
            result = self._make_request("POST", "/claims", data=claim_data)
            return {
                "success": True,
                "message": "Claim created successfully",
                "claim": result
            }
        except Exception as e:
            logger.error(f"Error creating claim: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_claim(self, claim_id: str, claim_data: Dict) -> Dict:
        """Update an existing claim"""
        try:
            # Remove claim_id from data (it's in the URL)
            claim_data = {k: v for k, v in claim_data.items() if k != "claim_id"}
            result = self._make_request("PUT", f"/claims/{claim_id}", data=claim_data)
            return {
                "success": True,
                "message": "Claim updated successfully",
                "claim": result
            }
        except Exception as e:
            logger.error(f"Error updating claim: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claim_proc(self, claim_proc_id: str) -> Dict:
        """Get a single claim procedure by ID"""
        try:
            result = self._make_request("GET", f"/claimprocs/{claim_proc_id}")
            return {
                "success": True,
                "claim_proc": result
            }
        except Exception as e:
            logger.error(f"Error getting claim procedure: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claim_procs(self, proc_num: Optional[str] = None, claim_num: Optional[str] = None, patient_id: Optional[str] = None, status: Optional[str] = None, claim_payment_num: Optional[str] = None) -> Dict:
        """Get claim procedures by criteria"""
        try:
            params = {}
            if proc_num:
                params["ProcNum"] = proc_num
            if claim_num:
                params["ClaimNum"] = claim_num
            if patient_id:
                params["PatNum"] = patient_id
            if status:
                params["Status"] = status
            if claim_payment_num:
                params["ClaimPaymentNum"] = claim_payment_num
            
            result = self._make_request("GET", "/claimprocs", params=params)
            return {
                "success": True,
                "claim_procs": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting claim procedures: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_claim_proc(self, claim_proc_id: str, claim_proc_data: Dict) -> Dict:
        """Update a claim procedure"""
        try:
            # Remove claim_proc_id from data (it's in the URL)
            claim_proc_data = {k: v for k, v in claim_proc_data.items() if k != "claim_proc_id"}
            result = self._make_request("PUT", f"/claimprocs/{claim_proc_id}", data=claim_proc_data)
            return {
                "success": True,
                "message": "Claim procedure updated successfully",
                "claim_proc": result
            }
        except Exception as e:
            logger.error(f"Error updating claim procedure: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claim_payment(self, claim_payment_id: str) -> Dict:
        """Get a single claim payment by ID"""
        try:
            result = self._make_request("GET", f"/claimpayments/{claim_payment_id}")
            return {
                "success": True,
                "claim_payment": result
            }
        except Exception as e:
            logger.error(f"Error getting claim payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_claim_payments(self, sec_date_t_edit: Optional[str] = None) -> Dict:
        """Get claim payments by criteria"""
        try:
            params = {}
            if sec_date_t_edit:
                params["SecDateTEdit"] = sec_date_t_edit
            
            result = self._make_request("GET", "/claimpayments", params=params)
            return {
                "success": True,
                "claim_payments": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting claim payments: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_claim_payment(self, payment_data: Dict) -> Dict:
        """Create a new claim payment"""
        try:
            result = self._make_request("POST", "/claimpayments", data=payment_data)
            return {
                "success": True,
                "message": "Claim payment created successfully",
                "claim_payment": result
            }
        except Exception as e:
            logger.error(f"Error creating claim payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_claim_payment_batch(self, payment_data: Dict) -> Dict:
        """Create a batch claim payment"""
        try:
            result = self._make_request("POST", "/claimpayments/Batch", data=payment_data)
            return {
                "success": True,
                "message": "Batch claim payment created successfully",
                "claim_payment": result
            }
        except Exception as e:
            logger.error(f"Error creating batch claim payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_claim_payment(self, claim_payment_id: str, payment_data: Dict) -> Dict:
        """Update an existing claim payment"""
        try:
            # Remove claim_payment_id from data (it's in the URL)
            payment_data = {k: v for k, v in payment_data.items() if k != "claim_payment_id"}
            result = self._make_request("PUT", f"/claimpayments/{claim_payment_id}", data=payment_data)
            return {
                "success": True,
                "message": "Claim payment updated successfully",
                "claim_payment": result
            }
        except Exception as e:
            logger.error(f"Error updating claim payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _delete_claim_payment(self, claim_payment_id: str) -> Dict:
        """Delete a claim payment"""
        try:
            url = f"{self.api_url}/claimpayments/{claim_payment_id}"
            response = self.session.delete(url)
            response.raise_for_status()
            if response.status_code == 204 or len(response.content) == 0:
                return {
                    "success": True,
                    "message": "Claim payment deleted successfully"
                }
            return {
                "success": True,
                "message": "Claim payment deleted successfully",
                "response": response.json() if response.content else None
            }
        except Exception as e:
            logger.error(f"Error deleting claim payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_plan(self, plan_id: str) -> Dict:
        """Get a single insurance plan by ID"""
        try:
            result = self._make_request("GET", f"/insplans/{plan_id}")
            return {
                "success": True,
                "insurance_plan": result
            }
        except Exception as e:
            logger.error(f"Error getting insurance plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_plans(self, plan_type: Optional[str] = None, carrier_num: Optional[str] = None) -> Dict:
        """Get insurance plans by criteria"""
        try:
            params = {}
            if plan_type:
                params["PlanType"] = plan_type
            if carrier_num:
                params["CarrierNum"] = carrier_num
            
            result = self._make_request("GET", "/insplans", params=params)
            return {
                "success": True,
                "insurance_plans": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting insurance plans: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_insurance_plan(self, plan_data: Dict) -> Dict:
        """Create a new insurance plan"""
        try:
            result = self._make_request("POST", "/insplans", data=plan_data)
            return {
                "success": True,
                "message": "Insurance plan created successfully",
                "insurance_plan": result
            }
        except Exception as e:
            logger.error(f"Error creating insurance plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_insurance_plan(self, plan_id: str, plan_data: Dict) -> Dict:
        """Update an existing insurance plan"""
        try:
            # Remove plan_id from data (it's in the URL)
            plan_data = {k: v for k, v in plan_data.items() if k != "plan_id"}
            result = self._make_request("PUT", f"/insplans/{plan_id}", data=plan_data)
            return {
                "success": True,
                "message": "Insurance plan updated successfully",
                "insurance_plan": result
            }
        except Exception as e:
            logger.error(f"Error updating insurance plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_subscription(self, ins_sub_id: str) -> Dict:
        """Get a single insurance subscription by ID"""
        try:
            result = self._make_request("GET", f"/inssubs/{ins_sub_id}")
            return {
                "success": True,
                "insurance_subscription": result
            }
        except Exception as e:
            logger.error(f"Error getting insurance subscription: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_subscriptions(self, plan_num: Optional[str] = None, subscriber: Optional[str] = None, sec_date_t_edit: Optional[str] = None) -> Dict:
        """Get insurance subscriptions by criteria"""
        try:
            params = {}
            if plan_num:
                params["PlanNum"] = plan_num
            if subscriber:
                params["Subscriber"] = subscriber
            if sec_date_t_edit:
                params["SecDateTEdit"] = sec_date_t_edit
            
            result = self._make_request("GET", "/inssubs", params=params)
            return {
                "success": True,
                "insurance_subscriptions": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting insurance subscriptions: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_insurance_subscription(self, subscription_data: Dict) -> Dict:
        """Create a new insurance subscription"""
        try:
            result = self._make_request("POST", "/inssubs", data=subscription_data)
            return {
                "success": True,
                "message": "Insurance subscription created successfully",
                "insurance_subscription": result
            }
        except Exception as e:
            logger.error(f"Error creating insurance subscription: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_insurance_subscription(self, ins_sub_id: str, subscription_data: Dict) -> Dict:
        """Update an existing insurance subscription"""
        try:
            # Remove ins_sub_id from data (it's in the URL)
            subscription_data = {k: v for k, v in subscription_data.items() if k != "ins_sub_id"}
            result = self._make_request("PUT", f"/inssubs/{ins_sub_id}", data=subscription_data)
            return {
                "success": True,
                "message": "Insurance subscription updated successfully",
                "insurance_subscription": result
            }
        except Exception as e:
            logger.error(f"Error updating insurance subscription: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _delete_insurance_subscription(self, ins_sub_id: str) -> Dict:
        """Delete an insurance subscription"""
        try:
            url = f"{self.api_url}/inssubs/{ins_sub_id}"
            response = self.session.delete(url)
            response.raise_for_status()
            if response.status_code == 204 or len(response.content) == 0:
                return {
                    "success": True,
                    "message": "Insurance subscription deleted successfully"
                }
            return {
                "success": True,
                "message": "Insurance subscription deleted successfully",
                "response": response.json() if response.content else None
            }
        except Exception as e:
            logger.error(f"Error deleting insurance subscription: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_verification(self, ins_verify_id: str) -> Dict:
        """Get a single insurance verification by ID"""
        try:
            result = self._make_request("GET", f"/insverifies/{ins_verify_id}")
            return {
                "success": True,
                "insurance_verification": result
            }
        except Exception as e:
            logger.error(f"Error getting insurance verification: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_insurance_verifications(self, verify_type: Optional[str] = None, f_key: Optional[str] = None, sec_date_t_edit: Optional[str] = None) -> Dict:
        """Get insurance verifications by criteria"""
        try:
            params = {}
            if verify_type:
                params["VerifyType"] = verify_type
            if f_key:
                params["FKey"] = f_key
            if sec_date_t_edit:
                params["SecDateTEdit"] = sec_date_t_edit
            
            result = self._make_request("GET", "/insverifies", params=params)
            return {
                "success": True,
                "insurance_verifications": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting insurance verifications: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_insurance_verification(self, verification_data: Dict) -> Dict:
        """Update an insurance verification"""
        try:
            result = self._make_request("PUT", "/insverifies", data=verification_data)
            return {
                "success": True,
                "message": "Insurance verification updated successfully",
                "insurance_verification": result
            }
        except Exception as e:
            logger.error(f"Error updating insurance verification: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_payments(self, pay_type: Optional[str] = None, patient_id: Optional[str] = None, date_entry: Optional[str] = None) -> Dict:
        """Get payments by criteria"""
        try:
            params = {}
            if pay_type:
                params["PayType"] = pay_type
            if patient_id:
                params["PatNum"] = patient_id
            if date_entry:
                params["DateEntry"] = date_entry
            
            result = self._make_request("GET", "/payments", params=params)
            return {
                "success": True,
                "payments": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting payments: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_payment(self, payment_data: Dict) -> Dict:
        """Create a new payment"""
        try:
            result = self._make_request("POST", "/payments", data=payment_data)
            return {
                "success": True,
                "message": "Payment created successfully",
                "payment": result
            }
        except Exception as e:
            logger.error(f"Error creating payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_payment_refund(self, pay_num: str) -> Dict:
        """Create a refund payment"""
        try:
            result = self._make_request("POST", "/payments/Refund", data={"PayNum": pay_num})
            return {
                "success": True,
                "message": "Refund payment created successfully",
                "payment": result
            }
        except Exception as e:
            logger.error(f"Error creating refund payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_payment(self, pay_num: str, payment_data: Dict) -> Dict:
        """Update an existing payment"""
        try:
            # Remove pay_num from data (it's in the URL)
            payment_data = {k: v for k, v in payment_data.items() if k != "pay_num"}
            result = self._make_request("PUT", f"/payments/{pay_num}", data=payment_data)
            return {
                "success": True,
                "message": "Payment updated successfully",
                "payment": result
            }
        except Exception as e:
            logger.error(f"Error updating payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_payment_partial(self, pay_num: str, payment_data: Dict) -> Dict:
        """Update payment allocation (partial)"""
        try:
            # Remove pay_num from data (it's in the URL)
            payment_data = {k: v for k, v in payment_data.items() if k != "pay_num"}
            result = self._make_request("PUT", f"/payments/{pay_num}/Partial", data=payment_data)
            return {
                "success": True,
                "message": "Payment allocation updated successfully",
                "payment": result
            }
        except Exception as e:
            logger.error(f"Error updating payment allocation: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_pay_splits(self, patient_id: Optional[str] = None, pay_num: Optional[str] = None, proc_num: Optional[str] = None) -> Dict:
        """Get pay splits by criteria"""
        try:
            params = {}
            if patient_id:
                params["PatNum"] = patient_id
            if pay_num:
                params["PayNum"] = pay_num
            if proc_num:
                params["ProcNum"] = proc_num
            
            result = self._make_request("GET", "/paysplits", params=params)
            return {
                "success": True,
                "pay_splits": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting pay splits: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_pay_split(self, split_num: str, split_data: Dict) -> Dict:
        """Update an existing pay split"""
        try:
            # Remove split_num from data (it's in the URL)
            split_data = {k: v for k, v in split_data.items() if k != "split_num"}
            result = self._make_request("PUT", f"/paysplits/{split_num}", data=split_data)
            return {
                "success": True,
                "message": "Pay split updated successfully",
                "pay_split": result
            }
        except Exception as e:
            logger.error(f"Error updating pay split: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_payment_plan(self, pay_plan_num: str) -> Dict:
        """Get a single payment plan by ID"""
        try:
            result = self._make_request("GET", f"/payplans/{pay_plan_num}")
            return {
                "success": True,
                "payment_plan": result
            }
        except Exception as e:
            logger.error(f"Error getting payment plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_payment_plans(self, patient_id: Optional[str] = None, guarantor: Optional[str] = None) -> Dict:
        """Get payment plans by patient or guarantor"""
        try:
            params = {}
            if patient_id:
                params["PatNum"] = patient_id
            if guarantor:
                params["Guarantor"] = guarantor
            
            result = self._make_request("GET", "/payplans", params=params)
            return {
                "success": True,
                "payment_plans": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting payment plans: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_payment_plan_dynamic(self, plan_data: Dict) -> Dict:
        """Create a new dynamic payment plan"""
        try:
            result = self._make_request("POST", "/payplans/Dynamic", data=plan_data)
            return {
                "success": True,
                "message": "Dynamic payment plan created successfully",
                "payment_plan": result
            }
        except Exception as e:
            logger.error(f"Error creating dynamic payment plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_treatment_plans(self, patient_id: Optional[str] = None, tp_status: Optional[str] = None, sec_date_t_edit: Optional[str] = None) -> Dict:
        """Get treatment plans by criteria"""
        try:
            params = {}
            if patient_id:
                params["PatNum"] = patient_id
            if tp_status:
                params["TPStatus"] = tp_status
            if sec_date_t_edit:
                params["SecDateTEdit"] = sec_date_t_edit
            
            result = self._make_request("GET", "/treatplans", params=params)
            return {
                "success": True,
                "treatment_plans": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting treatment plans: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_treatment_plan(self, plan_data: Dict) -> Dict:
        """Create a new inactive treatment plan"""
        try:
            result = self._make_request("POST", "/treatplans", data=plan_data)
            return {
                "success": True,
                "message": "Treatment plan created successfully",
                "treatment_plan": result
            }
        except Exception as e:
            logger.error(f"Error creating treatment plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_treatment_plan_saved(self, plan_data: Dict) -> Dict:
        """Create a saved treatment plan from existing plan"""
        try:
            result = self._make_request("POST", "/treatplans/Saved", data=plan_data)
            return {
                "success": True,
                "message": "Saved treatment plan created successfully",
                "treatment_plan": result
            }
        except Exception as e:
            logger.error(f"Error creating saved treatment plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_treatment_plan(self, treat_plan_num: str, plan_data: Dict) -> Dict:
        """Update a treatment plan"""
        try:
            # Remove treat_plan_num from data (it's in the URL)
            plan_data = {k: v for k, v in plan_data.items() if k != "treat_plan_num"}
            result = self._make_request("PUT", f"/treatplans/{treat_plan_num}", data=plan_data)
            return {
                "success": True,
                "message": "Treatment plan updated successfully",
                "treatment_plan": result
            }
        except Exception as e:
            logger.error(f"Error updating treatment plan: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_treatment_plan_procedures(self, treat_plan_num: str) -> Dict:
        """Get treatment plan procedures by treatment plan ID"""
        try:
            params = {"TreatPlanNum": treat_plan_num}
            result = self._make_request("GET", "/proctps", params=params)
            return {
                "success": True,
                "treatment_plan_procedures": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting treatment plan procedures: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _update_treatment_plan_procedure(self, proc_tp_num: str, proc_data: Dict) -> Dict:
        """Update a treatment plan procedure"""
        try:
            # Remove proc_tp_num from data (it's in the URL)
            proc_data = {k: v for k, v in proc_data.items() if k != "proc_tp_num"}
            result = self._make_request("PUT", f"/proctps/{proc_tp_num}", data=proc_data)
            return {
                "success": True,
                "message": "Treatment plan procedure updated successfully",
                "treatment_plan_procedure": result
            }
        except Exception as e:
            logger.error(f"Error updating treatment plan procedure: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _delete_treatment_plan_procedure(self, proc_tp_num: str) -> Dict:
        """Delete a treatment plan procedure"""
        try:
            url = f"{self.api_url}/proctps/{proc_tp_num}"
            response = self.session.delete(url)
            response.raise_for_status()
            if response.status_code == 204 or len(response.content) == 0:
                return {
                    "success": True,
                    "message": "Treatment plan procedure deleted successfully"
                }
            return {
                "success": True,
                "message": "Treatment plan procedure deleted successfully",
                "response": response.json() if response.content else None
            }
        except Exception as e:
            logger.error(f"Error deleting treatment plan procedure: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _create_treatment_plan_procedure(self, proc_data: Dict) -> Dict:
        """Add a procedure directly to a specific treatment plan.

        Creates a treatment plan procedure (ProcTP) via POST /proctps. This allows
        adding procedures to specific inactive or active treatment plans, enabling
        alternative treatment plan workflows (e.g., Plan A vs Plan B).

        Args:
            proc_data: Dict containing at minimum TreatPlanNum, PatNum, and ProcCode.
                Optional fields: ToothNumTP, Surf, Descript, FeeAmt, PriInsAmt,
                SecInsAmt, PatAmt, Discount, Priority, Prognosis, Dx, ProvNum,
                DateTP, ClinicNum, ItemOrder.

        Returns:
            Dict with success status and the created procedure data.
        """
        try:
            # Validate required fields
            if not proc_data.get("TreatPlanNum"):
                return {
                    "success": False,
                    "error": "TreatPlanNum is required — specify which treatment plan to add the procedure to"
                }
            if not proc_data.get("PatNum"):
                return {
                    "success": False,
                    "error": "PatNum is required"
                }
            if not proc_data.get("ProcCode"):
                return {
                    "success": False,
                    "error": "ProcCode is required (e.g., 'D6010', 'D2393')"
                }

            # Build the API payload with only recognized ProcTP fields
            api_fields = [
                "TreatPlanNum", "PatNum", "ProcCode", "ToothNumTP", "Surf",
                "Descript", "FeeAmt", "PriInsAmt", "SecInsAmt", "PatAmt",
                "Discount", "Priority", "Prognosis", "Dx", "ProcAbbr",
                "ProvNum", "DateTP", "ClinicNum", "ItemOrder", "ProcNumOrig",
                "FeeAllowed"
            ]
            payload = {k: v for k, v in proc_data.items() if k in api_fields and v is not None and v != ""}

            result = self._make_request("POST", "/proctps", data=payload)
            return {
                "success": True,
                "message": f"Procedure added to treatment plan {proc_data.get('TreatPlanNum')} (ProcCode: {proc_data.get('ProcCode')})",
                "treatment_plan_procedure": result
            }
        except Exception as e:
            logger.error(f"Error creating treatment plan procedure: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _get_patient_info(self, patient_id: str) -> Dict:
        """Get patient information from Chart Module"""
        try:
            result = self._make_request("GET", f"/chartmodules/{patient_id}/PatientInfo")
            return {
                "success": True,
                "patient_info": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting patient info: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_progress_notes(self, patient_id: str, offset: Optional[int] = None) -> Dict:
        """Get progress notes from Chart Module"""
        try:
            params = {}
            if offset is not None:
                params["Offset"] = offset
            
            result = self._make_request("GET", f"/chartmodules/{patient_id}/ProgNotes", params=params)
            return {
                "success": True,
                "progress_notes": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting progress notes: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_planned_appointments(self, patient_id: str) -> Dict:
        """Get planned appointments from Chart Module"""
        try:
            result = self._make_request("GET", f"/chartmodules/{patient_id}/PlannedAppts")
            return {
                "success": True,
                "planned_appointments": result if isinstance(result, list) else [result],
                "count": len(result) if isinstance(result, list) else 1
            }
        except Exception as e:
            logger.error(f"Error getting planned appointments: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_document(self, document_data: Dict) -> Dict:
        """Create a new document record"""
        try:
            # Set default date if not provided
            if "DateCreated" not in document_data:
                document_data["DateCreated"] = datetime.now().strftime("%Y-%m-%d")
            
            # Try database method first (if configured)
            if self.db_server and self.db_database:
                logger.info("Attempting document creation via database...")
                patient_id = str(document_data.get("PatNum", ""))
                file_name = document_data.get("FileName", "")
                description = document_data.get("Description", "")
                category = document_data.get("DocCategory", 0)
                
                # If RawBase64 is provided, use upload method
                if "RawBase64" in document_data:
                    return self._upload_document_via_db(patient_id, file_name, document_data["RawBase64"], description, category)
                
                # Otherwise, create document record without file
                conn = self._get_db_connection()
                if not conn:
                    return {
                        "success": False,
                        "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."
                    }
                
                cursor = conn.cursor()
                
                if self.db_type == "sqlserver":
                    cursor.execute("SELECT ISNULL(MAX(DocNum), 0) + 1 FROM document")
                    doc_num = cursor.fetchone()[0]
                    
                    cursor.execute("""
                        INSERT INTO document (DocNum, PatNum, FileName, DateCreated, Description, DocCategory)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        doc_num,
                        int(patient_id),
                        file_name,
                        document_data["DateCreated"],
                        description,
                        category
                    ))
                
                elif self.db_type == "mysql":
                    cursor.execute("SELECT COALESCE(MAX(DocNum), 0) + 1 FROM document")
                    doc_num = cursor.fetchone()[0]
                    
                    cursor.execute("""
                        INSERT INTO document (DocNum, PatNum, FileName, DateCreated, Description, DocCategory)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        doc_num,
                        int(patient_id),
                        file_name,
                        document_data["DateCreated"],
                        description,
                        category
                    ))
                
                conn.commit()
                cursor.close()
                conn.close()
                
                return {
                    "success": True,
                    "message": "Document created successfully via database",
                    "document": {
                        "DocNum": doc_num,
                        "PatNum": int(patient_id),
                        "FileName": file_name
                    }
                }
            
            # Fall back to REST API (which will fail, but provides clear error)
            result = self._make_request("POST", "/documents", data=document_data)
            return {
                "success": True,
                "message": "Document created successfully",
                "document": result
            }
        except Exception as e:
            logger.error(f"Error creating document: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _create_procnote(self, proc_num: Any, pat_num: Any, note: Any, user_num: Any = 0) -> Dict:
        """Create a clinical note row in procnote for a completed procedure."""
        try:
            # Validate required values and note text quality first.
            if proc_num is None or pat_num is None:
                return {
                    "success": False,
                    "error": "proc_num and pat_num are required"
                }

            note_text = str(note).strip() if note is not None else ""
            if not note_text:
                return {
                    "success": False,
                    "error": "note cannot be empty"
                }

            proc_num_int = int(proc_num)
            pat_num_int = int(pat_num)
            user_num_int = int(user_num) if user_num is not None else 0

            conn = self._get_db_connection()
            if not conn:
                return {
                    "success": False,
                    "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."
                }

            cursor = conn.cursor()
            now_dt = datetime.now()
            note_preview = note_text[:50]
            logger.info(
                "create_procnote write at %s PatNum=%s ProcNum=%s Note[0:50]=%s",
                now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                pat_num_int,
                proc_num_int,
                note_preview,
            )

            if self.db_type == "sqlserver":
                verify_sql = """
                    SELECT ProcNum, PatNum, ProcStatus
                    FROM procedurelog
                    WHERE ProcNum = ? AND PatNum = ? AND ProcStatus = 2
                """
                cursor.execute(verify_sql, (proc_num_int, pat_num_int))
            elif self.db_type == "mysql":
                verify_sql = """
                    SELECT ProcNum, PatNum, ProcStatus
                    FROM procedurelog
                    WHERE ProcNum = %s AND PatNum = %s AND ProcStatus = 2
                """
                cursor.execute(verify_sql, (proc_num_int, pat_num_int))
            else:
                cursor.close()
                conn.close()
                return {
                    "success": False,
                    "error": f"Unsupported database type for create_procnote: {self.db_type}"
                }

            proc_row = cursor.fetchone()
            if not proc_row:
                cursor.close()
                conn.close()
                return {
                    "success": False,
                    "error": "Procedure not found for this patient, or procedure is not completed (ProcStatus must be 2)."
                }

            entry_dt = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            if self.db_type == "sqlserver":
                insert_sql = """
                    INSERT INTO procnote (PatNum, ProcNum, EntryDateTime, UserNum, Note, SigIsTopaz, Signature)
                    VALUES (?, ?, ?, ?, ?, 0, '')
                """
                cursor.execute(insert_sql, (pat_num_int, proc_num_int, entry_dt, user_num_int, note_text))
                cursor.execute("SELECT ISNULL(MAX(ProcNoteNum), 0) FROM procnote WHERE ProcNum = ? AND PatNum = ?", (proc_num_int, pat_num_int))
                proc_note_num = cursor.fetchone()[0]
            else:
                insert_sql = """
                    INSERT INTO procnote (PatNum, ProcNum, EntryDateTime, UserNum, Note, SigIsTopaz, Signature)
                    VALUES (%s, %s, %s, %s, %s, 0, '')
                """
                cursor.execute(insert_sql, (pat_num_int, proc_num_int, entry_dt, user_num_int, note_text))
                proc_note_num = getattr(cursor, "lastrowid", None)
                if not proc_note_num:
                    cursor.execute("SELECT COALESCE(MAX(ProcNoteNum), 0) FROM procnote WHERE ProcNum = %s AND PatNum = %s", (proc_num_int, pat_num_int))
                    proc_note_num = cursor.fetchone()[0]

            conn.commit()
            cursor.close()
            conn.close()

            return {
                "success": True,
                "message": "Clinical note created successfully",
                "procnote": {
                    "ProcNoteNum": int(proc_note_num) if proc_note_num is not None else None,
                    "PatNum": pat_num_int,
                    "ProcNum": proc_num_int,
                    "UserNum": user_num_int,
                    "EntryDateTime": entry_dt,
                }
            }
        except Exception as e:
            logger.error(f"Error creating procnote: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_db_connection(self):
        """Get database connection for direct database access"""
        if not self.db_server or not self.db_database:
            return None
        
        try:
            if self.db_type == "sqlserver":
                if not DB_AVAILABLE:
                    raise Exception("pyodbc not installed - install with: pip install pyodbc")
                
                if self.db_use_windows_auth:
                    conn_str = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={self.db_server};DATABASE={self.db_database};Trusted_Connection=yes;"
                else:
                    conn_str = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={self.db_server};DATABASE={self.db_database};UID={self.db_username};PWD={self.db_password};"
                
                return pyodbc.connect(conn_str)
            
            elif self.db_type == "mysql":
                if not MYSQL_AVAILABLE:
                    raise Exception("pymysql not installed - install with: pip install pymysql")
                
                # Enable multi-statement execution for complex queries
                return pymysql.connect(
                    host=self.db_server,
                    database=self.db_database,
                    user=self.db_username,
                    password=self.db_password,
                    client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS
                )
            
            else:
                logger.warning(f"Unknown database type: {self.db_type}")
                return None
        except Exception as e:
            logger.error(f"Error connecting to database: {e}")
            return None
    
    def _query_database(self, query: str, limit: int = 1000) -> Dict:
        """Execute a query - tries API first, then database for complex queries"""
        try:
            if not query or not query.strip():
                return {
                    "success": False,
                    "error": "Query cannot be empty"
                }
            
            query = query.strip()
            
            # Strip SQL comments (-- style) for detection purposes
            # Remove single-line comments (-- comment)
            lines = query.split('\n')
            stripped_lines = []
            for line in lines:
                # Find -- comment marker (but not inside strings)
                comment_pos = line.find('--')
                if comment_pos >= 0:
                    # Check if it's inside a string
                    before_comment = line[:comment_pos]
                    single_quotes = before_comment.count("'") - before_comment.count("\\'")
                    double_quotes = before_comment.count('"') - before_comment.count('\\"')
                    if single_quotes % 2 == 0 and double_quotes % 2 == 0:
                        # Not inside a string, remove comment
                        line = line[:comment_pos].rstrip()
                stripped_lines.append(line)
            query_no_comments = '\n'.join(stripped_lines).strip()
            
            query_upper = query_no_comments.upper()
            
            # Try to use API endpoints first for simple queries
            # Check if query looks like an API endpoint path
            if query.startswith("/"):
                # It's an API endpoint path
                try:
                    # Parse endpoint and params
                    if "?" in query:
                        endpoint, params_str = query.split("?", 1)
                        # Simple param parsing (can be improved)
                        params = {}
                        for param in params_str.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                params[key] = value
                    else:
                        endpoint = query
                        params = {}
                    
                    # Make API request
                    # Try with params first, if it fails, try as path parameter
                    try:
                        result = self._make_request("GET", endpoint, params=params)
                    except Exception as param_error:
                        # If query params failed, try treating the query as a direct path
                        # e.g., /patients/123 instead of /patients?PatNum=123
                        if params and endpoint in ["/patients", "/appointments", "/providers", "/labcases", "/documents"]:
                            # Try to extract ID from params and use as path
                            for key in ["PatNum", "AptNum", "ProvNum", "LabCaseNum", "DocNum", "id"]:
                                if key in params:
                                    endpoint = f"{endpoint}/{params[key]}"
                                    params = {}
                                    result = self._make_request("GET", endpoint, params=params)
                                    break
                            else:
                                raise param_error
                        else:
                            raise param_error
                    
                    # Convert to standard format
                    if isinstance(result, list):
                        rows = result[:limit]
                    elif isinstance(result, dict):
                        # Check if it's a single record or a collection
                        if "PatNum" in result or "AptNum" in result or "ProvNum" in result:
                            rows = [result]
                        else:
                            # Might be a collection with a key
                            for key in ["patients", "appointments", "providers", "laboratories", "labcases", "documents", "procedurecodes"]:
                                if key in result:
                                    rows = result[key][:limit] if isinstance(result[key], list) else [result[key]]
                                    break
                            else:
                                rows = [result]
                    else:
                        rows = [result] if result else []
                    
                    # Convert to standard format
                    if rows and isinstance(rows[0], dict):
                        columns = list(rows[0].keys())
                    else:
                        columns = []
                    
                    return {
                        "success": True,
                        "query": query,
                        "method": "API",
                        "columns": columns,
                        "rows": rows[:limit],
                        "row_count": len(rows[:limit]),
                        "limited": len(rows) > limit,
                        "message": f"Query executed via API. Returned {len(rows[:limit])} row(s)."
                    }
                except Exception as api_error:
                    logger.warning(f"API query failed, trying database: {api_error}")
                    # Fall through to database query
            
            # Check if it's a SQL query (starts with SELECT, INSERT, UPDATE, DELETE, etc.)
            # Include SET for MySQL user variables, and handle multi-statement queries
            is_sql = any(query_upper.startswith(keyword) for keyword in ["SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "CREATE", "ALTER", "DROP", "SET"])
            
            if is_sql:
                # SQL query - requires database access
                if not self.db_server or not self.db_database:
                    return {
                        "success": False,
                        "error": "SQL queries require database configuration. Set OPENDENTAL_DB_* environment variables. For simple queries, try using API endpoints (e.g., '/patients?PatNum=123')."
                    }
                
                # Security: Warn about non-SELECT queries
                if not query_upper.startswith("SELECT"):
                    logger.warning(f"Non-SELECT query detected: {query[:50]}...")
                
                # Limit results
                if limit > 10000:
                    limit = 10000
                if limit < 1:
                    limit = 1000
                
                # Connect to database
                conn = self._get_db_connection()
                if not conn:
                    return {
                        "success": False,
                        "error": "Failed to connect to database. Check database configuration."
                    }
                
                cursor = conn.cursor()
                
                # Execute query
                try:
                    # Handle multi-statement queries (e.g., SET variables, CREATE TEMP TABLE, SELECT)
                    # Split by semicolon but preserve semicolons inside strings/quotes
                    statements = []
                    current_statement = ""
                    in_string = False
                    string_char = None
                    
                    for char in query:
                        if char in ("'", '"', '`') and not (current_statement and current_statement[-1] == '\\'):
                            if not in_string:
                                in_string = True
                                string_char = char
                            elif char == string_char:
                                in_string = False
                                string_char = None
                        elif char == ';' and not in_string:
                            stmt = current_statement.strip()
                            if stmt:
                                statements.append(stmt)
                            current_statement = ""
                            continue
                        current_statement += char
                    
                    # Add final statement if exists
                    if current_statement.strip():
                        statements.append(current_statement.strip())
                    
                    # If no semicolons found, treat as single statement
                    if not statements:
                        statements = [query]
                    
                    # For multi-statement queries, execute all statements together
                    # This preserves UNION ALL, user variables, and temporary tables
                    if len(statements) > 1:
                        # Execute all statements together (multi-statement mode)
                        # pymysql with MULTI_STATEMENTS flag can handle this
                        full_query = '; '.join(statements)
                        cursor.execute(full_query)
                        
                        # For multi-statement queries, we need to iterate through results
                        # The last result set is what we want (the SELECT)
                        columns = []
                        rows = []
                        result_set_count = 0
                        while True:
                            try:
                                # Get column names from current result set
                                if cursor.description:
                                    columns = [column[0] for column in cursor.description]
                                    rows = cursor.fetchmany(limit)
                                    if rows:
                                        # This is a result set with data - use it
                                        break
                                # Move to next result set
                                if not cursor.nextset():
                                    break
                                result_set_count += 1
                            except Exception:
                                break
                        
                        # If we didn't get results, try fetching from current cursor
                        if not rows and cursor.description:
                            columns = [column[0] for column in cursor.description]
                            rows = cursor.fetchmany(limit)
                    else:
                        # Single statement - execute normally
                        cursor.execute(query)
                        columns = [column[0] for column in cursor.description] if cursor.description else []
                        rows = cursor.fetchmany(limit)
                    
                    # Convert rows to dictionaries
                    results = []
                    for row in rows:
                        row_dict = {}
                        for i, col in enumerate(columns):
                            value = row[i]
                            # Convert datetime and other special types to strings
                            if hasattr(value, 'isoformat'):
                                value = value.isoformat()
                            elif value is None:
                                value = None
                            row_dict[col] = value
                        results.append(row_dict)
                    
                    # Get total count (if possible, for SELECT queries)
                    total_count = len(results)
                    if total_count == limit and query_upper.startswith("SELECT"):
                        # Might be more results
                        try:
                            # Try to get count
                            count_query = f"SELECT COUNT(*) as total FROM ({query}) as subquery"
                            cursor.execute(count_query)
                            count_row = cursor.fetchone()
                            if count_row:
                                total_count = count_row[0] if isinstance(count_row[0], int) else count_row[0]
                        except:
                            pass  # Count query might fail, use row count
                    
                    cursor.close()
                    conn.close()
                    
                    return {
                        "success": True,
                        "query": query,
                        "method": "Database",
                        "columns": columns,
                        "rows": results,
                        "row_count": len(results),
                        "total_count": total_count if total_count != len(results) else None,
                        "limited": len(results) == limit,
                        "message": f"Query executed via database. Returned {len(results)} row(s)."
                    }
                    
                except Exception as query_error:
                    cursor.close()
                    conn.close()
                    error_msg = str(query_error)
                    logger.error(f"Query execution error: {error_msg}")
                    return {
                        "success": False,
                        "error": f"Query execution failed: {error_msg}",
                        "query": query
                    }
            else:
                # Natural language or unknown format - try to help
                return {
                    "success": False,
                    "error": f"Query format not recognized. Use: 1) API endpoint (e.g., '/patients?PatNum=123'), 2) SQL query (e.g., 'SELECT * FROM patient WHERE PatNum = 123'), or 3) Natural language query (coming soon).",
                    "suggestions": [
                        "Try: '/patients?PatNum=123' for API query",
                        "Try: 'SELECT * FROM patient WHERE PatNum = 123' for SQL query",
                        "For SQL queries, database configuration is required"
                    ]
                }
                
        except Exception as e:
            logger.error(f"Error in query_database: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_schema_knowledge(self) -> Dict:
        """Get Open Dental schema knowledge for query generation"""
        return {
            "tables": {
                "patient": {
                    "description": "Patient information",
                    "key_columns": ["PatNum", "FName", "LName", "Birthdate", "SSN", "Phone", "Email", "Address", "City", "State", "Zip"],
                    "common_filters": ["LName", "FName", "Phone", "Email", "Birthdate"]
                },
                "appointment": {
                    "description": "Appointment scheduling",
                    "key_columns": ["AptNum", "PatNum", "AptDateTime", "ProvNum", "ProvHyg", "AptStatus", "Pattern", "ProcDescript"],
                    "common_filters": ["PatNum", "AptDateTime", "ProvNum", "ProvHyg", "AptStatus"],
                    "relationships": {"patient": "PatNum"}
                },
                "procedurelog": {
                    "description": "Completed procedures",
                    "key_columns": ["ProcNum", "PatNum", "ProcDate", "CodeNum", "ToothNum", "Surf", "ProvNum"],
                    "common_filters": ["PatNum", "ProcDate", "CodeNum", "ProvNum"],
                    "relationships": {"patient": "PatNum"},
                    "note": "Use CodeNum, not ProcCode (ProcCode doesn't exist)"
                },
                "claim": {
                    "description": "Insurance claims",
                    "key_columns": ["ClaimNum", "PatNum", "ClaimDate", "DateSent", "ClaimStatus"],
                    "common_filters": ["PatNum", "ClaimDate", "ClaimStatus"],
                    "relationships": {"patient": "PatNum"}
                },
                "payment": {
                    "description": "Payments received",
                    "key_columns": ["PayNum", "PatNum", "PayDate", "PayAmt", "PayType"],
                    "common_filters": ["PatNum", "PayDate"],
                    "relationships": {"patient": "PatNum"}
                },
                "provider": {
                    "description": "Provider information",
                    "key_columns": ["ProvNum", "Abbr", "FName", "LName", "IsHidden"],
                    "common_filters": ["Abbr", "FName", "LName"]
                },
                "procedurecode": {
                    "description": "Procedure code definitions",
                    "key_columns": ["CodeNum", "ProcCode", "Descript", "AbbrDesc"],
                    "common_filters": ["ProcCode", "Descript"]
                }
            },
            "common_patterns": {
                "patient_search": "SELECT PatNum, FName, LName FROM patient WHERE {conditions}",
                "appointments_by_date": "SELECT a.* FROM appointment a WHERE a.AptDateTime BETWEEN '{start}' AND '{end}'",
                "procedures_by_patient": "SELECT p.* FROM procedurelog p WHERE p.PatNum = {patnum}",
                "date_functions": {
                    "mysql": {
                        "now": "NOW()",
                        "today": "CURDATE()",
                        "date_add": "DATE_ADD({date}, INTERVAL {amount} {unit})",
                        "date_sub": "DATE_SUB({date}, INTERVAL {amount} {unit})",
                        "date_format": "DATE_FORMAT({date}, '%Y-%m-%d')"
                    },
                    "sqlserver": {
                        "now": "GETDATE()",
                        "today": "CAST(GETDATE() AS DATE)",
                        "date_add": "DATEADD({unit}, {amount}, {date})",
                        "date_sub": "DATEADD({unit}, -{amount}, {date})",
                        "date_format": "FORMAT({date}, 'yyyy-MM-dd')"
                    }
                }
            },
            "column_patterns": {
                "patient_id": ["PatNum"],
                "appointment_id": ["AptNum"],
                "procedure_id": ["ProcNum"],
                "provider_id": ["ProvNum"],
                "date_columns": ["Date", "DateTime", "DateCreated", "DateModified", "AptDateTime", "ProcDate", "PayDate", "ClaimDate"],
                "name_columns": ["FName", "LName", "FirstName", "LastName"],
                "status_columns": ["Status", "AptStatus", "ClaimStatus"]
            }
        }
    
    def _generate_sql_from_natural_language(self, description: str, schema_hints: Optional[List[str]] = None, previous_attempts: Optional[List[Dict]] = None) -> str:
        """Generate SQL query from natural language description using schema knowledge"""
        schema = self._get_schema_knowledge()
        db_type = self.db_type or "mysql"  # Default to MySQL
        
        # Extract bad columns from previous attempts
        bad_columns = set()
        if previous_attempts:
            import re
            for attempt in previous_attempts:
                error = attempt.get('error', '')
                if 'column' in error.lower() and ('not found' in error.lower() or 'unknown' in error.lower()):
                    col_match = re.search(r"['\"]([^'\"]+)['\"]", error)
                    if col_match:
                        bad_columns.add(col_match.group(1))
        
        # Build context from previous attempts
        context = ""
        if previous_attempts:
            context = "\n\nPrevious attempts:\n"
            for i, attempt in enumerate(previous_attempts[-3:], 1):  # Last 3 attempts
                context += f"\nAttempt {i}:\n"
                context += f"Query: {attempt.get('query', '')}\n"
                if attempt.get('error'):
                    context += f"Error: {attempt.get('error')}\n"
        
        # Extract key terms from description
        description_lower = description.lower()
        
        # Check if this is a COUNT query
        is_count_query = "count" in description_lower or "number of" in description_lower or "how many" in description_lower
        
        # Determine main table
        main_table = None
        if "patient" in description_lower or "patients" in description_lower:
            main_table = "patient"
        elif "appointment" in description_lower or "appointments" in description_lower:
            main_table = "appointment"
        elif "procedure" in description_lower or "procedures" in description_lower:
            main_table = "procedurelog"
        elif "claim" in description_lower or "claims" in description_lower:
            main_table = "claim"
        elif "payment" in description_lower or "payments" in description_lower:
            main_table = "payment"
        elif "provider" in description_lower or "providers" in description_lower:
            main_table = "provider"
        
        if not main_table:
            main_table = "patient"  # Default
        
        table_info = schema["tables"].get(main_table, {})
        key_columns = table_info.get("key_columns", [])
        
        # Build WHERE clause based on description
        where_conditions = []
        
        # Date filters
        if "today" in description_lower or "todays" in description_lower:
            date_func = schema["common_patterns"]["date_functions"][db_type]["today"]
            if main_table == "appointment":
                # Use exact date match for "today" if "scheduled for today" is mentioned
                if "scheduled for today" in description_lower or "for today" in description_lower:
                    where_conditions.append(f"DATE(AptDateTime) = {date_func}")
                else:
                    where_conditions.append(f"AptDateTime >= {date_func}")
            elif "Date" in str(key_columns):
                date_col = next((c for c in key_columns if "Date" in c), None)
                if date_col:
                    where_conditions.append(f"{date_col} >= {date_func}")
        
        if "next week" in description_lower or "next 7 days" in description_lower:
            date_func = schema["common_patterns"]["date_functions"][db_type]["date_add"]
            if db_type == "mysql":
                where_conditions.append(f"AptDateTime >= DATE_ADD(NOW(), INTERVAL 7 DAY)")
            else:
                where_conditions.append(f"AptDateTime >= DATEADD(day, 7, GETDATE())")
        
        if "last week" in description_lower or "past week" in description_lower:
            date_func = schema["common_patterns"]["date_functions"][db_type]["date_sub"]
            if db_type == "mysql":
                where_conditions.append(f"AptDateTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)")
            else:
                where_conditions.append(f"AptDateTime >= DATEADD(day, -7, GETDATE())")
        
        # Name filters - improved pattern matching
        import re
        # Pattern 1: "last name X" or "with last name X"
        last_name_match = re.search(r"(?:with\s+)?last\s+name\s+([A-Z][a-zA-Z]+)", description, re.IGNORECASE)
        if last_name_match:
            name = last_name_match.group(1)
            where_conditions.append(f"LName = '{name}'")
        else:
            # Pattern 2: "named X" or "name is X" or "called X"
            name_match = re.search(r"(?:named|name is|called|with name)\s+([A-Z][a-zA-Z]+)", description, re.IGNORECASE)
            if name_match:
                name = name_match.group(1)
                if "last" in description_lower or "surname" in description_lower:
                    where_conditions.append(f"LName = '{name}'")
                elif "first" in description_lower:
                    where_conditions.append(f"FName = '{name}'")
                else:
                    where_conditions.append(f"(FName = '{name}' OR LName = '{name}')")
            else:
                # Pattern 3: "patients X" where X might be a name
                # This is less reliable, so we'll be conservative
                pass
        
        # Status filters
        if "overdue" in description_lower:
            if main_table == "payment":
                where_conditions.append("PayDate < DATE_SUB(NOW(), INTERVAL 90 DAY)")
            elif main_table == "claim":
                where_conditions.append("ClaimStatus != 'Received'")
        
        # Build SELECT clause - use only columns that exist in schema and avoid bad columns
        # Handle COUNT queries
        if is_count_query:
            # Use plural form for alias
            alias_map = {
                "patient": "TotalPatients",
                "appointment": "TotalAppointments",
                "procedurelog": "TotalProcedures",
                "provider": "TotalProviders",
                "claim": "TotalClaims",
                "payment": "TotalPayments"
            }
            alias = alias_map.get(main_table, f"Total{main_table.capitalize()}")
            query = f"SELECT COUNT(*) as {alias} FROM {main_table}"
            if where_conditions:
                query += " WHERE " + " AND ".join(where_conditions)
            return query
        
        # Regular SELECT queries
        if main_table == "patient":
            # Use only columns from schema, excluding bad columns from previous attempts
            safe_columns = [c for c in key_columns if c not in bad_columns]
            if safe_columns:
                # Use first few safe columns
                select_cols = ", ".join(safe_columns[:5])
            else:
                # Fallback to basic columns that should always exist
                select_cols = "PatNum, FName, LName"
        elif main_table == "appointment":
            select_cols = "AptNum, PatNum, AptDateTime, ProvNum, AptStatus"
        elif main_table == "procedurelog":
            # Use CodeNum instead of ProcCode (which doesn't exist)
            # Fee column doesn't exist, so exclude it
            safe_cols = [c for c in key_columns if c not in bad_columns and c != "Fee"]
            if "CodeNum" in safe_cols:
                select_cols = "ProcNum, PatNum, ProcDate, CodeNum"
            else:
                select_cols = ", ".join([c for c in safe_cols if c not in bad_columns and c != "Fee"][:5])
        elif main_table == "provider":
            # Add IsHidden filter for "list all providers" queries
            if "list" in description_lower and "all" in description_lower:
                where_conditions.append("IsHidden = 0")
            select_cols = "ProvNum, Abbr, FName, LName"
        else:
            # For unknown tables, use key columns from schema
            if key_columns:
                safe_cols = [c for c in key_columns if c not in bad_columns]
                select_cols = ", ".join(safe_cols[:5])  # First 5 columns
            else:
                select_cols = "*"
        
        # Build query
        query = f"SELECT {select_cols} FROM {main_table}"
        if where_conditions:
            query += " WHERE " + " AND ".join(where_conditions)
        
        # Add LIMIT for MySQL or TOP for SQL Server (unless it's a COUNT query)
        if not is_count_query:
            if db_type == "mysql":
                query += " LIMIT 1000"
            else:
                query = query.replace("SELECT ", "SELECT TOP 1000 ", 1)
        
        return query
    
    def _analyze_query_error(self, error: str, query: str, description: str) -> Dict:
        """Analyze query error and suggest fixes"""
        error_lower = error.lower()
        schema = self._get_schema_knowledge()
        db_type = self.db_type or "mysql"
        
        analysis = {
            "error_type": "unknown",
            "suggested_fix": None,
            "issue": error
        }
        
        # Column not found
        if "column" in error_lower and ("not found" in error_lower or "unknown" in error_lower):
            analysis["error_type"] = "column_not_found"
            # Try to extract column name from error
            import re
            col_match = re.search(r"['\"]([^'\"]+)['\"]", error)
            if col_match:
                bad_column = col_match.group(1)
                analysis["bad_column"] = bad_column
                # Extract table name from query if possible
                query_lower = query.lower()
                table_match = re.search(r"from\s+(\w+)", query_lower)
                if table_match:
                    table_name = table_match.group(1)
                    table_info = schema["tables"].get(table_name, {})
                    valid_columns = table_info.get("key_columns", [])
                    if valid_columns:
                        analysis["suggested_fix"] = f"Column '{bad_column}' doesn't exist in table '{table_name}'. Available columns: {', '.join(valid_columns[:10])}"
                        analysis["valid_columns"] = valid_columns
                else:
                    # Suggest columns from all tables
                    all_columns = []
                    for table_info in schema["tables"].values():
                        all_columns.extend(table_info.get("key_columns", []))
                    analysis["suggested_fix"] = f"Column '{bad_column}' not found. Common columns: {', '.join(set(all_columns)[:10])}"
        
        # Table not found
        elif "table" in error_lower and ("not found" in error_lower or "doesn't exist" in error_lower):
            analysis["error_type"] = "table_not_found"
            analysis["suggested_fix"] = f"Available tables: {', '.join(schema['tables'].keys())}"
        
        # Syntax error
        elif "syntax" in error_lower or "syntax error" in error_lower:
            analysis["error_type"] = "syntax_error"
            # Check for common syntax issues
            if "union" in query.lower() and "order by" in query.lower():
                analysis["suggested_fix"] = "In UNION queries, ORDER BY should be at the end, after all SELECT statements"
            elif db_type == "mysql" and "top" in query.lower():
                analysis["suggested_fix"] = "MySQL uses LIMIT instead of TOP. Replace 'TOP N' with 'LIMIT N' at the end"
            elif db_type == "sqlserver" and "limit" in query.lower():
                analysis["suggested_fix"] = "SQL Server uses TOP instead of LIMIT. Replace 'LIMIT N' with 'SELECT TOP N'"
        
        # Type mismatch
        elif "type" in error_lower and ("mismatch" in error_lower or "incompatible" in error_lower):
            analysis["error_type"] = "type_mismatch"
            analysis["suggested_fix"] = "Check data types in WHERE clause. Use CAST() or CONVERT() if needed"
        
        return analysis
    
    def _detect_complexity(self, description: str) -> Dict:
        """Detect if query is simple, complex, or unknown"""
        description_lower = description.lower()
        
        # Complex query indicators
        complex_patterns = [
            r"\bwith\b.*\b(appointments?|procedures?|claims?|payments?)\b",  # "patients with appointments"
            r"\bwho\s+have\b",  # "patients who have appointments"
            r"\bthat\s+have\b",  # "providers that have appointments"
            r"\band\s+also\b",  # "patients with X and also Y"
            r"\beach\b",  # "each provider", "each patient"
            r"\bper\b",  # "appointments per provider"
            r"\bgroup\s+by\b",  # Explicit GROUP BY mention
            r"\bjoin\b",  # Explicit JOIN mention
            r"\bcount.*each\b",  # "count appointments each provider"
            r"\bshow.*for\s+each\b",  # "show appointments for each provider"
        ]
        
        # Check for multiple tables mentioned
        tables_mentioned = []
        table_keywords = {
            "patient": ["patient", "patients"],
            "appointment": ["appointment", "appointments", "scheduled", "scheduling"],
            "procedure": ["procedure", "procedures", "completed", "treatment"],
            "provider": ["provider", "providers", "doctor", "dentist"],
            "claim": ["claim", "claims", "insurance"],
            "payment": ["payment", "payments", "paid", "balance"]
        }
        
        for table_name, keywords in table_keywords.items():
            if any(keyword in description_lower for keyword in keywords):
                tables_mentioned.append(table_name)
        
        # Check for complex patterns
        has_complex_pattern = any(
            __import__('re').search(pattern, description_lower) 
            for pattern in complex_patterns
        )
        
        # Determine complexity
        if len(tables_mentioned) > 1 and has_complex_pattern:
            complexity = "complex"
            reason = f"Query mentions multiple tables ({', '.join(tables_mentioned)}) and requires JOINs or aggregations"
        elif len(tables_mentioned) > 1:
            complexity = "likely_complex"
            reason = f"Query mentions multiple tables ({', '.join(tables_mentioned)}) - may require JOINs"
        elif has_complex_pattern:
            complexity = "likely_complex"
            reason = "Query pattern suggests JOINs or aggregations (e.g., 'with', 'each', 'per')"
        elif len(tables_mentioned) == 1 and not has_complex_pattern:
            complexity = "simple"
            reason = "Single table query with simple filters"
        else:
            complexity = "unknown"
            reason = "Cannot determine complexity - will attempt pattern-based generation"
        
        return {
            "complexity": complexity,
            "reason": reason,
            "tables_mentioned": tables_mentioned,
            "has_complex_pattern": has_complex_pattern
        }
    
    def _suggest_sql_for_complex_query(self, description: str, complexity_info: Dict) -> str:
        """Generate a suggested SQL query for complex queries (for user reference, not execution)"""
        tables = complexity_info.get("tables_mentioned", [])
        description_lower = description.lower()
        
        # Basic JOIN suggestion based on common patterns
        if "patient" in tables and "appointment" in tables:
            if "next week" in description_lower or "next 7 days" in description_lower:
                return """
SELECT DISTINCT p.PatNum, p.FName, p.LName, a.AptDateTime
FROM patient p
INNER JOIN appointment a ON p.PatNum = a.PatNum
WHERE a.AptDateTime >= CURDATE() 
    AND a.AptDateTime <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)
    AND a.AptStatus != 5
LIMIT 1000
"""
            else:
                return """
SELECT DISTINCT p.PatNum, p.FName, p.LName, a.AptDateTime
FROM patient p
INNER JOIN appointment a ON p.PatNum = a.PatNum
WHERE a.AptStatus != 5
LIMIT 1000
"""
        
        elif "provider" in tables and "appointment" in tables:
            if "count" in description_lower or "each" in description_lower:
                return """
SELECT p.ProvNum, p.Abbr, p.FName, p.LName, COUNT(a.AptNum) as AppointmentCount
FROM provider p
LEFT JOIN appointment a ON (p.ProvNum = a.ProvNum OR p.ProvNum = a.ProvHyg)
    AND a.AptStatus != 5
WHERE p.IsHidden = 0
GROUP BY p.ProvNum, p.Abbr, p.FName, p.LName
ORDER BY AppointmentCount DESC
LIMIT 1000
"""
        
        elif "patient" in tables and "procedure" in tables:
            return """
SELECT DISTINCT p.PatNum, p.FName, p.LName, pr.ProcDate, pr.CodeNum
FROM patient p
INNER JOIN procedurelog pr ON p.PatNum = pr.PatNum
WHERE pr.ProcDate >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
LIMIT 1000
"""
        
        # Generic suggestion
        return f"""
-- Suggested SQL for: {description}
-- This query requires JOINs between: {', '.join(tables)}
-- Example structure:
SELECT ...
FROM {tables[0] if tables else 'table1'} t1
INNER JOIN {tables[1] if len(tables) > 1 else 'table2'} t2 ON t1.PatNum = t2.PatNum
WHERE ...
LIMIT 1000
"""
    
    def _smart_query(self, description: str, max_iterations: Optional[int] = None, read_only: Optional[bool] = None, validate_results: bool = True, schema_hints: Optional[List[str]] = None) -> Dict:
        """Self-iterating query tool that automatically generates, executes, and fixes SQL queries"""
        import time
        
        max_iterations = max_iterations or self.query_iterator_max_iterations
        read_only = read_only if read_only is not None else self.query_iterator_read_only
        
        # Step 0: Check for dangerous operations in read-only mode
        if read_only:
            description_lower = description.lower()
            dangerous_keywords = ["delete", "drop", "truncate", "update", "insert", "alter", "create table", "modify", "remove", "clear"]
            if any(keyword in description_lower for keyword in dangerous_keywords):
                return {
                    "success": False,
                    "error": "Read-only mode: Query description contains dangerous keywords (DELETE, UPDATE, INSERT, DROP, etc.). Only SELECT queries allowed.",
                    "iterations": 0,
                    "history": [],
                    "read_only": True
                }
        
        # Step 1: Detect complexity before attempting generation
        complexity_info = self._detect_complexity(description)
        complexity = complexity_info.get("complexity", "unknown")
        
        # If query is clearly complex, fail fast with helpful message
        if complexity == "complex":
            suggested_sql = self._suggest_sql_for_complex_query(description, complexity_info)
            return {
                "success": False,
                "error": f"Query is too complex for pattern-based generation. {complexity_info.get('reason', '')}",
                "complexity": complexity,
                "complexity_info": complexity_info,
                "suggestion": "Use query_database tool directly with SQL",
                "suggested_sql": suggested_sql.strip(),
                "iterations": 0,
                "history": [],
                "reasoning": "Complexity detected before generation - query requires JOINs or aggregations that pattern-based generation cannot handle."
            }
        
        # If query is likely complex, warn but still try
        if complexity == "likely_complex":
            logger.info(f"Query likely complex: {complexity_info.get('reason')} - attempting pattern-based generation")
        
        history = []
        start_time = time.time()
        
        current_query = None
        for iteration in range(1, max_iterations + 1):
            try:
                # Generate SQL query
                if iteration == 1:
                    # First attempt: generate from natural language
                    current_query = self._generate_sql_from_natural_language(description, schema_hints)
                else:
                    # Subsequent attempts: fix based on previous error
                    last_attempt = history[-1]
                    error_analysis = self._analyze_query_error(
                        last_attempt.get("error", ""),
                        last_attempt.get("query", ""),
                        description
                    )
                    
                    # Generate new query with fix
                    current_query = self._generate_sql_from_natural_language(
                        description,
                        schema_hints,
                        previous_attempts=history
                    )
                    
                    # Apply suggested fixes
                    if error_analysis.get("suggested_fix"):
                        # Fix column not found errors
                        if error_analysis["error_type"] == "column_not_found":
                            bad_column = error_analysis.get("bad_column")
                            valid_columns = error_analysis.get("valid_columns", [])
                            if bad_column and valid_columns:
                                # Replace bad column with first valid column, or remove it
                                import re
                                # Remove the bad column from SELECT clause
                                current_query = re.sub(
                                    rf",\s*{bad_column}\b|{bad_column}\s*,|{bad_column}\b",
                                    "",
                                    current_query,
                                    flags=re.IGNORECASE
                                )
                                # If SELECT clause becomes empty, add a default column
                                if re.search(r"SELECT\s+FROM", current_query, re.IGNORECASE):
                                    current_query = current_query.replace("SELECT FROM", f"SELECT {valid_columns[0]} FROM")
                        
                        # Fix syntax errors
                        elif error_analysis["error_type"] == "syntax_error":
                            if "LIMIT" in error_analysis["suggested_fix"]:
                                # Replace TOP with LIMIT for MySQL
                                if self.db_type == "mysql":
                                    current_query = re.sub(r"SELECT\s+TOP\s+\d+\s+", "SELECT ", current_query, flags=re.IGNORECASE)
                                    if "LIMIT" not in current_query.upper():
                                        current_query += " LIMIT 1000"
                            elif "TOP" in error_analysis["suggested_fix"]:
                                # Replace LIMIT with TOP for SQL Server
                                if self.db_type == "sqlserver":
                                    limit_match = re.search(r"LIMIT\s+(\d+)", current_query, re.IGNORECASE)
                                    if limit_match:
                                        limit_num = limit_match.group(1)
                                        current_query = re.sub(r"LIMIT\s+\d+", "", current_query, flags=re.IGNORECASE)
                                        current_query = re.sub(r"SELECT\s+", f"SELECT TOP {limit_num} ", current_query, count=1, flags=re.IGNORECASE)
                
                # Validate query is read-only if required
                if read_only:
                    query_upper = current_query.upper().strip()
                    dangerous_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE TABLE"]
                    if any(keyword in query_upper for keyword in dangerous_keywords):
                        return {
                            "success": False,
                            "error": f"Read-only mode: Query contains dangerous keywords. Only SELECT queries allowed.",
                            "iterations": iteration,
                            "history": history
                        }
                
                # Execute query
                result = self._query_database(current_query, limit=self.query_iterator_max_rows)
                
                # Record attempt
                attempt = {
                    "iteration": iteration,
                    "query": current_query,
                    "timestamp": time.time()
                }
                
                if result.get("success"):
                    # Success!
                    attempt["status"] = "success"
                    attempt["row_count"] = result.get("row_count", 0)
                    
                    # Validate results if requested
                    if validate_results:
                        row_count = result.get("row_count", 0)
                        if row_count == 0:
                            # Empty results - might be unexpected
                            attempt["warning"] = "Query executed successfully but returned 0 rows. This might be expected or might indicate missing filters."
                        elif row_count > 1000:
                            attempt["warning"] = f"Query returned {row_count} rows. Consider adding more specific filters."
                    
                    history.append(attempt)
                    
                    execution_time = time.time() - start_time
                    return {
                        "success": True,
                        "iterations": iteration,
                        "final_query": current_query,
                        "results": result.get("rows", []),
                        "metadata": {
                            "row_count": result.get("row_count", 0),
                            "execution_time": round(execution_time, 2),
                            "method": result.get("method", "unknown"),
                            "columns": result.get("columns", [])
                        },
                        "history": history,
                        "reasoning": f"Query successfully generated and executed after {iteration} iteration(s)."
                    }
                else:
                    # Error occurred
                    error_msg = result.get("error", "Unknown error")
                    attempt["status"] = "error"
                    attempt["error"] = error_msg
                    
                    # Analyze error
                    error_analysis = self._analyze_query_error(error_msg, current_query, description)
                    attempt["error_analysis"] = error_analysis
                    
                    history.append(attempt)
                    
                    # Check if we should continue
                    if iteration >= max_iterations:
                        execution_time = time.time() - start_time
                        
                        # Check if failure suggests complex query (JOIN needed)
                        error_lower = error_msg.lower()
                        needs_join = (
                            "column" in error_lower and "not found" in error_lower and 
                            ("appointment" in error_lower or "procedure" in error_lower or "join" in error_lower)
                        ) or (
                            complexity == "likely_complex"
                        )
                        
                        response = {
                            "success": False,
                            "error": f"Query failed after {max_iterations} iterations. Last error: {error_msg}",
                            "iterations": iteration,
                            "final_query": current_query,
                            "history": history,
                            "reasoning": f"Unable to generate valid query after {max_iterations} attempts. See history for details."
                        }
                        
                        # If failure suggests complex query, provide helpful suggestion
                        if needs_join:
                            suggested_sql = self._suggest_sql_for_complex_query(description, complexity_info)
                            response["suggestion"] = "This query likely requires JOINs. Use query_database tool directly with SQL"
                            response["suggested_sql"] = suggested_sql.strip()
                            response["complexity_detected"] = True
                        
                        return response
                    
                    # Continue to next iteration
                    logger.info(f"Query iteration {iteration} failed, retrying... Error: {error_msg}")
                    
            except Exception as e:
                logger.error(f"Error in smart_query iteration {iteration}: {e}")
                history.append({
                    "iteration": iteration,
                    "query": current_query or "N/A",
                    "status": "exception",
                    "error": str(e)
                })
                
                if iteration >= max_iterations:
                    return {
                        "success": False,
                        "error": f"Query failed after {max_iterations} iterations. Exception: {str(e)}",
                        "iterations": iteration,
                        "history": history
                    }
        
        # Should not reach here, but just in case
        return {
            "success": False,
            "error": "Query iteration loop completed unexpectedly",
            "iterations": max_iterations,
            "history": history
        }
    
    def _upload_document_via_db(self, patient_id: str, file_name: str, file_data: str, description: str, category: int) -> Dict:
        """Upload document via direct database access"""
        try:
            # Decode base64 file data
            file_bytes = base64.b64decode(file_data)
            
            # Write file to AtoZ folder if path is configured
            file_path = None
            if self.atoz_path:
                import os as os_module
                # Create patient-specific folder in AtoZ (Open Dental convention)
                patient_folder = os_module.path.join(self.atoz_path, str(patient_id))
                os_module.makedirs(patient_folder, exist_ok=True)
                
                # Write file
                file_path = os_module.path.join(patient_folder, file_name)
                with open(file_path, 'wb') as f:
                    f.write(file_bytes)
                
                logger.info(f"File written to: {file_path}")
            
            # Insert document record into database
            conn = self._get_db_connection()
            if not conn:
                return {
                    "success": False,
                    "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."
                }
            
            cursor = conn.cursor()
            
            # Get next DocNum (Open Dental uses auto-increment)
            # For SQL Server
            if self.db_type == "sqlserver":
                cursor.execute("SELECT ISNULL(MAX(DocNum), 0) + 1 FROM document")
                doc_num = cursor.fetchone()[0]
                
                # Insert document record
                cursor.execute("""
                    INSERT INTO document (DocNum, PatNum, FileName, DateCreated, Description, DocCategory, RawBase64)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    doc_num,
                    int(patient_id),
                    file_name,
                    datetime.now().strftime("%Y-%m-%d"),
                    description,
                    category,
                    file_data  # Store base64 in database (Open Dental convention)
                ))
            
            # For MySQL
            elif self.db_type == "mysql":
                cursor.execute("SELECT COALESCE(MAX(DocNum), 0) + 1 FROM document")
                doc_num = cursor.fetchone()[0]
                
                cursor.execute("""
                    INSERT INTO document (DocNum, PatNum, FileName, DateCreated, Description, DocCategory, RawBase64)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    doc_num,
                    int(patient_id),
                    file_name,
                    datetime.now().strftime("%Y-%m-%d"),
                    description,
                    category,
                    file_data
                ))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return {
                "success": True,
                "message": "Document uploaded successfully via database",
                "document": {
                    "DocNum": doc_num,
                    "PatNum": int(patient_id),
                    "FileName": file_name,
                    "FilePath": file_path
                }
            }
            
        except Exception as e:
            logger.error(f"Error uploading document via database: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _upload_document(self, upload_data: Dict) -> Dict:
        """Upload a document file (base64 encoded)"""
        try:
            patient_id = upload_data.get("patient_id")
            file_name = upload_data.get("file_name")
            file_data = upload_data.get("file_data")
            description = upload_data.get("description", "")
            category = upload_data.get("category", 0)
            
            if not patient_id or not file_name or not file_data:
                return {
                    "success": False,
                    "error": "patient_id, file_name, and file_data are required"
                }
            
            # Try database upload first (if configured)
            if self.db_server and self.db_database:
                logger.info("Attempting document upload via database...")
                return self._upload_document_via_db(patient_id, file_name, file_data, description, category)
            
            # Fall back to REST API (which will fail, but provides clear error)
            document_data = {
                "PatNum": int(patient_id),
                "FileName": file_name,
                "DateCreated": datetime.now().strftime("%Y-%m-%d"),
                "Description": description,
                "DocCategory": category,
                "RawBase64": file_data
            }
            
            result = self._make_request("POST", "/documents", data=document_data)
            return {
                "success": True,
                "message": "Document uploaded successfully",
                "document": result
            }
        except Exception as e:
            logger.error(f"Error uploading document: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    # ── Popup tools (direct DB) ──────────────────────────────────

    def _get_popups(self, patient_id: Optional[str] = None, include_disabled: bool = False,
                    description_contains: Optional[str] = None) -> Dict:
        """Get popup alerts, optionally filtered by patient and/or description."""
        try:
            conn = self._get_db_connection()
            if not conn:
                return {"success": False, "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."}

            cursor = conn.cursor()
            conditions = []
            params = []
            ph = "?" if self.db_type == "sqlserver" else "%s"

            if not include_disabled:
                conditions.append("IsDisabled = 0")

            if patient_id:
                conditions.append(f"PatNum = {ph}")
                params.append(int(patient_id))

            if description_contains:
                conditions.append(f"Description LIKE {ph}")
                params.append(f"%{description_contains}%")

            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"SELECT PopupNum, PatNum, Description, IsDisabled, PopupLevel, UserNum, DateTimeEntry, IsArchived, DateTimeDisabled FROM popup{where} ORDER BY PopupNum DESC"

            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

            cursor.close()
            conn.close()

            # Stringify datetimes for JSON serialisation
            for row in rows:
                for k, v in row.items():
                    if isinstance(v, datetime):
                        row[k] = v.strftime("%Y-%m-%d %H:%M:%S")

            return {"success": True, "popups": rows, "count": len(rows)}

        except Exception as e:
            logger.error(f"Error in get_popups: {e}")
            return {"success": False, "error": str(e)}

    def _create_popup(self, data: Dict) -> Dict:
        """Create a single popup alert."""
        try:
            patient_id = data.get("patient_id") or data.get("PatNum")
            description = data.get("description", "")
            popup_level = int(data.get("popup_level", 0))

            if not patient_id or not description:
                return {"success": False, "error": "patient_id and description are required"}

            conn = self._get_db_connection()
            if not conn:
                return {"success": False, "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."}

            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if self.db_type == "sqlserver":
                cursor.execute("SELECT ISNULL(MAX(PopupNum), 0) + 1 FROM popup")
                popup_num = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO popup (PopupNum, PatNum, Description, IsDisabled, PopupLevel, UserNum, DateTimeEntry, IsArchived, PopupNumArchive, DateTimeDisabled)
                    VALUES (?, ?, ?, 0, ?, 0, ?, 0, 0, '0001-01-01')
                """, (popup_num, int(patient_id), description, popup_level, now_str))
            elif self.db_type == "mysql":
                cursor.execute("SELECT COALESCE(MAX(PopupNum), 0) + 1 FROM popup")
                popup_num = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO popup (PopupNum, PatNum, Description, IsDisabled, PopupLevel, UserNum, DateTimeEntry, IsArchived, PopupNumArchive, DateTimeDisabled)
                    VALUES (%s, %s, %s, 0, %s, 0, %s, 0, 0, '0001-01-01')
                """, (popup_num, int(patient_id), description, popup_level, now_str))
            else:
                cursor.close()
                conn.close()
                return {"success": False, "error": f"Unsupported db_type: {self.db_type}"}

            conn.commit()
            cursor.close()
            conn.close()

            return {
                "success": True,
                "message": "Popup created successfully",
                "popup": {"PopupNum": popup_num, "PatNum": int(patient_id), "Description": description, "PopupLevel": popup_level}
            }

        except Exception as e:
            logger.error(f"Error in create_popup: {e}")
            return {"success": False, "error": str(e)}

    def _create_popups_batch(self, popups: List[Dict]) -> Dict:
        """Create multiple popup alerts in a single transaction."""
        try:
            if not popups:
                return {"success": False, "error": "popups array is empty"}

            conn = self._get_db_connection()
            if not conn:
                return {"success": False, "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."}

            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Get starting PopupNum
            if self.db_type == "sqlserver":
                cursor.execute("SELECT ISNULL(MAX(PopupNum), 0) FROM popup")
            elif self.db_type == "mysql":
                cursor.execute("SELECT COALESCE(MAX(PopupNum), 0) FROM popup")
            else:
                cursor.close()
                conn.close()
                return {"success": False, "error": f"Unsupported db_type: {self.db_type}"}

            next_num = cursor.fetchone()[0] + 1

            created = []
            errors = []

            for i, p in enumerate(popups):
                pat_id = p.get("patient_id") or p.get("PatNum")
                desc = p.get("description", "")
                level = int(p.get("popup_level", 0))

                if not pat_id or not desc:
                    errors.append({"index": i, "error": "missing patient_id or description"})
                    continue

                popup_num = next_num + len(created)
                try:
                    if self.db_type == "sqlserver":
                        cursor.execute("""
                            INSERT INTO popup (PopupNum, PatNum, Description, IsDisabled, PopupLevel, UserNum, DateTimeEntry, IsArchived, PopupNumArchive, DateTimeDisabled)
                            VALUES (?, ?, ?, 0, ?, 0, ?, 0, 0, '0001-01-01')
                        """, (popup_num, int(pat_id), desc, level, now_str))
                    else:
                        cursor.execute("""
                            INSERT INTO popup (PopupNum, PatNum, Description, IsDisabled, PopupLevel, UserNum, DateTimeEntry, IsArchived, PopupNumArchive, DateTimeDisabled)
                            VALUES (%s, %s, %s, 0, %s, 0, %s, 0, 0, '0001-01-01')
                        """, (popup_num, int(pat_id), desc, level, now_str))
                    created.append({"PopupNum": popup_num, "PatNum": int(pat_id), "Description": desc})
                except Exception as insert_err:
                    errors.append({"index": i, "PatNum": pat_id, "error": str(insert_err)})

            conn.commit()
            cursor.close()
            conn.close()

            return {
                "success": True,
                "message": f"Created {len(created)} popup(s)",
                "created_count": len(created),
                "error_count": len(errors),
                "created": created,
                "errors": errors if errors else None
            }

        except Exception as e:
            logger.error(f"Error in create_popups_batch: {e}")
            return {"success": False, "error": str(e)}

    def _disable_popups(self, patient_id: Optional[str] = None,
                        description_contains: Optional[str] = None,
                        disable_all_matching: bool = False) -> Dict:
        """Disable popup alerts matching criteria."""
        try:
            if not patient_id and not description_contains:
                return {"success": False, "error": "At least one filter (patient_id or description_contains) is required"}

            if description_contains and not patient_id and not disable_all_matching:
                return {
                    "success": False,
                    "error": "Set disable_all_matching=true to confirm bulk disable by description without a patient_id filter"
                }

            conn = self._get_db_connection()
            if not conn:
                return {"success": False, "error": "Database connection not configured. Set OPENDENTAL_DB_* environment variables."}

            cursor = conn.cursor()
            conditions = ["IsDisabled = 0"]
            params = []
            ph = "?" if self.db_type == "sqlserver" else "%s"

            if patient_id:
                conditions.append(f"PatNum = {ph}")
                params.append(int(patient_id))

            if description_contains:
                conditions.append(f"Description LIKE {ph}")
                params.append(f"%{description_contains}%")

            where = " AND ".join(conditions)

            if self.db_type == "sqlserver":
                sql = f"UPDATE popup SET IsDisabled = 1, DateTimeDisabled = GETDATE() WHERE {where}"
            else:
                sql = f"UPDATE popup SET IsDisabled = 1, DateTimeDisabled = NOW() WHERE {where}"

            cursor.execute(sql, params)
            affected = cursor.rowcount

            conn.commit()
            cursor.close()
            conn.close()

            return {
                "success": True,
                "message": f"Disabled {affected} popup(s)",
                "disabled_count": affected
            }

        except Exception as e:
            logger.error(f"Error in disable_popups: {e}")
            return {"success": False, "error": str(e)}

