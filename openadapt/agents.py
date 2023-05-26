from transformers import OpenAiAgent


class transformer_agent(OpenAiAgent):

    """
    This is an OpenAdapt decorator for HuggingFace's OpenAiAgent class.
    """

    screenshots = []

    def __init__(
        self,
        model,
        api_key,
        chat_prompt_template=None,
        run_prompt_template=None,
        additional_tools=None,
        screenshots=None,
    ):
        super().__init__(
            model, api_key, chat_prompt_template, run_prompt_template, additional_tools
        )
        self.screenshots = screenshots

    def prompt(self, prompt, img_src):
        response = self.run(prompt, image=img_src)
        return response
