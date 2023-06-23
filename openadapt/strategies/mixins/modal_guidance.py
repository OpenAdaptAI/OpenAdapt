import modal
import guidance
my_image = modal.Image.debian_slim().pip_install("guidance", "transformers", "torch", "torchvision", "torchaudio")


stub = modal.Stub()


@stub.function(gpu="any", image=my_image, timeout=1200)
def key_func():
    # use alpha bc it's the smallest
    guidance.llm = guidance.llms.Transformers("stabilityai/stablelm-base-alpha-3b")

    # we can pre-define valid option sets
    valid_medium = ["keyboard", "mouse"]

    # define the prompt
    program = guidance(""" {{description}} Given the above information, fill in the following (with N/A where unapplicable) as a valid json
    ```json
    {
        "medium": "{{select 'medium' options=valid_medium}}",
        "keyclick x-location": {{gen 'location' pattern='[0-9]+[0-9]+[0-9]+' stop=')'}},
        "keyclick y-location": {{gen 'location' pattern='[0-9]+[0-9]+[0-9]+' stop=')'}},
        "character": {{gen 'character' pattern='[a-z]+' stop=','}}
        ]
    }```""")

    # execute the prompt
    out = program(description="You want to close a browser window.", valid_medium=valid_medium)

    print(out)