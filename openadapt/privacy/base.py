from typing import List

from pydantic import BaseModel


class Modality(BaseModel):
    """A Base Class for Modality Types"""

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    PDF = "PDF"
    MP4 = "MP4"


class ScrubbingProvider(BaseModel):
    """A Base Class for Scrubbing Providers"""

    name: str
    capabilities: List[Modality]

    def scrub_text(self):
        raise NotImplementedError

    def scrub_image(self):
        raise NotImplementedError

    def scrub_pdf(self):
        raise NotImplementedError

    def scrub_mp4(self):
        raise NotImplementedError
