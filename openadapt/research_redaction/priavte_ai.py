from loguru import logger
import requests

from openadapt import config

url = "https://api.private-ai.com/deid/v3/process/text"

payload = {
    "text": ["hello my name is Bob Smith"],
    "link_batch": False,
    "entity_detection": {
        "accuracy": "high",
        "entity_types": [
            {
                "type": "ENABLE",
                "value": [
                    "ACCOUNT_NUMBER",
                    "AGE",
                    "DATE",
                    "DATE_INTERVAL",
                    "DOB",
                    "DRIVER_LICENSE",
                    "DURATION",
                    "EMAIL_ADDRESS",
                    "EVENT",
                    "FILENAME",
                    "GENDER_SEXUALITY",
                    "HEALTHCARE_NUMBER",
                    "IP_ADDRESS",
                    "LANGUAGE",
                    "LOCATION",
                    "LOCATION_ADDRESS",
                    "LOCATION_CITY",
                    "LOCATION_COORDINATE",
                    "LOCATION_COUNTRY",
                    "LOCATION_STATE",
                    "LOCATION_ZIP",
                    "MARITAL_STATUS",
                    "MONEY",
                    "NAME",
                    "NAME_FAMILY",
                    "NAME_GIVEN",
                    "NAME_MEDICAL_PROFESSIONAL",
                    "NUMERICAL_PII",
                    "ORGANIZATION",
                    "ORGANIZATION_MEDICAL_FACILITY",
                    "OCCUPATION",
                    "ORIGIN",
                    "PASSPORT_NUMBER",
                    "PASSWORD",
                    "PHONE_NUMBER",
                    "PHYSICAL_ATTRIBUTE",
                    "POLITICAL_AFFILIATION",
                    "RELIGION",
                    "SSN",
                    "TIME",
                    "URL",
                    "USERNAME",
                    "VEHICLE_ID",
                    "ZODIAC_SIGN",
                    "BLOOD_TYPE",
                    "CONDITION",
                    "DOSE",
                    "DRUG",
                    "INJURY",
                    "MEDICAL_PROCESS",
                    "STATISTICS",
                    "BANK_ACCOUNT",
                    "CREDIT_CARD",
                    "CREDIT_CARD_EXPIRATION",
                    "CVV",
                    "ROUTING_NUMBER",
                ],
            }
        ],
        "return_entity": True,
    },
    "processed_text": {"type": "MARKER", "pattern": "[UNIQUE_NUMBERED_ENTITY_TYPE]"},
}

headers = {
    "Content-Type": "application/json",
    "X-API-KEY": config.PRIVATE_AI_API_KEY,
}

response = requests.post(url, json=payload, headers=headers)

data = response.json()

logger.info(data)
logger.info(data[0].get("processed_text"))
