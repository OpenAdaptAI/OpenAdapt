from loguru import logger
from openadapt.ml.fine_tuning.openai.openai_finetune import *


def test_openai_finetune_on_recording():
    """
    A test to confirm whether the openai finetuning workflow
    works properly on a given recording.

    Please ensure that you have at least ONE recording completed
    using OpenAdapt before running this test.
    """

    davinci_fine_tuner = OpenAIFineTuner("davinci")

    file_path = davinci_fine_tuner.prepare_data_for_tuning(2)

    check_data_output = davinci_fine_tuner.check_data_for_tuning(file_path)

    logger.debug(f"{check_data_output}")

    tune_davinci = davinci_fine_tuner.tune_model(file_path)

    logger.debug(f"{tune_davinci}")

    # TODO: feed a new recording using StatefulReplayStrat and
    # use the fine-tuned model for inference.


if __name__ == "__main__":
    test_openai_finetune_on_recording()
