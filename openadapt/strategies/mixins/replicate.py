from PIL import Image
import replicate

from openadapt import config
from openadapt.strategies.base import BaseReplayStrategy


SAM_MODEL_VERSION = "pablodawson/segment-anything-automatic:14fbb04535964b3d0c7fad03bb4ed272130f15b956cbedb7b2f20b5b8a2dbaa0"

class ReplicateReplayStrategyMixin(BaseReplayStrategy):
    """Mixin class implementing Replicate.com API"""

    def get_segmentation(image: Image):

        output = replicate.run(
            "pablodawson/segment-anything-automatic:14fbb04535964b3d0c7fad03bb4ed272130f15b956cbedb7b2f20b5b8a2dbaa0",
            input={
                "image": "https://example.com/image.png"
            }
        )
        print(output)

        api = replicate.Client(api_token=config.REPLICATE_API_KEY)
        model = api.models.get(SAM_MODEL_VERSION)
        result = model.predict(image=image)[0]
        logger.info(f"result=\n{pformat(result)}")

        """
import { promises as fs } from "fs";

// Read the file into a buffer
const data = await fs.readFile("path/to/image.png");
// Convert the buffer into a base64-encoded string
const base64 = data.toString("base64");
// Set MIME type for PNG image
const mimeType = "image/png";
// Create the data URI
const dataURI = `data:${mimeType};base64,${base64}`;

const model = "nightmareai/real-esrgan:42fed1c4974146d4d2414e2be2c5277c7fcf05fcc3a73abf41610695738c1d7b";
const input = {
  image: dataURI,
};
const output = await replicate.run(model, { input });
// ['https://replicate.delivery/mgxm/e7b0e122-9daa-410e-8cde-006c7308ff4d/output.png']
        """
