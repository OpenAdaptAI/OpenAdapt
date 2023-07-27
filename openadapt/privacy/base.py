from typing import List

from pydantic import BaseModel


class Modality:
    """A Base Class for Modality Types"""

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    PDF = "PDF"
    MP4 = "MP4"


class ScrubbingProvider(BaseModel):
    """A Base Class for Scrubbing Providers"""

    name: str
    capabilities: List[Modality]

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, name: str, capabilities: List[Modality]) -> None:
        self.name = name
        self.capabilities = capabilities

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        raise NotImplementedError

    def scrub_image(self):
        raise NotImplementedError

    def scrub_pdf(self):
        raise NotImplementedError

    def scrub_mp4(self):
        raise NotImplementedError
