from anonymizer.anonymization.anonymizer import Anonymizer
from anonymizer.anonymization.config import AnonymizerConfig
from anonymizer.anonymization.pii import Pii, AnonymizedPii
from anonymizer.mechanisms.pseudonymization import (
    PseudonymizationParameters,
    Pseudonymization,
)

detected_pii = [
    Pii(tag="PERSON", text="David"),
    Pii(tag="PERSON", text="Larissa"),
    Pii(tag="PERSON", text="Mark"),
]

config = AnonymizerConfig(
    default_mechanism=Pseudonymization(),
    mechanisms_by_tag={
        "PERSON": Pseudonymization(format_string="Bar {}"),
    },
)

anonymizer = Anonymizer(config)
anonymized_pii = anonymizer.anonymize(detected_pii)

print(list(anonymized_pii))
