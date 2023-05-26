import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FNAME = "openadapt.db"

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DEFAULT_SCRUB_FILL_COLOR = (255,0,0)
SCRUB_IGNORE_ENTITIES = [
    # 'US_PASSPORT',
    # 'US_DRIVER_LICENSE',
    # 'CRYPTO',
    # 'UK_NHS',
    # 'PERSON',
    # 'CREDIT_CARD',
    # 'US_BANK_NUMBER',
    # 'PHONE_NUMBER',
    # 'US_ITIN',
    # 'AU_ABN',
    "DATE_TIME",
    # 'NRP',
    # 'SG_NRIC_FIN',
    # 'AU_ACN',
    # 'IP_ADDRESS',
    # 'EMAIL_ADDRESS',
    'URL',
    # 'IBAN_CODE',
    # 'AU_TFN',
    # 'LOCATION',
    # 'AU_MEDICARE',
    # 'US_SSN',
    # 'MEDICAL_LICENSE'
]
