import modal_plugin

def test_generate_completions(prompt: str, 
                        max_tokens: int, 
                        model_name: str) -> None:
    
    completions = modal_plugin.execute(prompt, max_tokens, model_name)
    print(completions)

if __name__ == "__main__":
    test_generate_completions("How much wood does a woodchuck chuck if a", 
                         512, "gpt2")