# first line: 76
@cache.cache()
def create_openai_completion(
    model,
    messages,
    # temperatere=1,
    # top_p=1,
    # n=1,
    # stream=False,
    # stop=None,
    # max_tokens=inf,
    # presence_penalty=0,
    # frequency_penalty=0,
    # logit_bias=None,
    # user=None,
):
    return openai.ChatCompletion.create(
        model=model,
        messages=messages,
        # temperatere=temperature,
        # top_p=top_p,
        # n=n,
        # stream=stream,
        # stop=stop,
        # max_tokens=max_tokens,
        # presence_penalty=presence_penalty,
        # frequency_penalty=frequency_penalty,
        # logit_bias=logit_bias,
        # user=user,
    )
