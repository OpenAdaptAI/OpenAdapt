import lmql

@lmql.query
def format(description: str):
    '''lmql
    argmax
        "{description} Summarize the action in the above description:"
        "action: [ACTION],"
        "character: [CHAR]"
    from
        "openai/davinci"
    where
        len(TOKENS(CHAR)) < 5 and ACTION in set(["move", "scroll", "type"]) and len(TOKENS(CHAR)) > 0
    '''

list_of_results = format("Penelope entered 'I' into the website.")

result = list_of_results[0]

filled_in_prompt = result.prompt

result_dict = result.variables

action_value = result_dict["ACTION"]
char_value = result_dict["CHAR"]

print(filled_in_prompt)
# result: Penelope entered 'I' into the website. Summarize the action in the above description:action: type,char: 

# "I

print(action_value)
# result: type
print(char_value)
# result: /n /n "I