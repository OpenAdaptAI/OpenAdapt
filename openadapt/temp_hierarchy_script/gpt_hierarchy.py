import openai
import os
from openadapt import config
openai.api_key = config.OPENAI_API_KEY

completion = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role":"system",
            "content": ("You are enumerating a hierarchy of actions "
                        "that one takes when operating GUI desktop "
                        "applications for typical day-to-day tasks. "
                        "Consider different levels of abstractions. "
                        "Examples include: clicking a button, opening "
                        "a window, operating payroll software, generating "
                        " invoices, renting an apartment.")},
        {"role":"user",
            "content":("Enumerate all of the different levels of this hierarchy, "
                        "starting from the least granular to the most. "
                        "Only separate each level by the word NEW, do NOT use any "
                        "new line characters between levels. Make sure that your output "
                        "consists ONLY of levels. You are not allowed to output anything "
                        "else.")}
    ]
)

levels = completion.choices[0].message['content'].split("NEW")

tasks_by_level = {}
for i in range(len(levels)):
    levelprompt = ("Enumerate all of the different tasks of this hierarchy "
                    "at the following level: {}.".format(levels[i]))

    completion_2 = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role":"system",
                "content": ("You are enumerating a hierarchy of actions "
                            "that one takes when operating GUI desktop "
                            "applications for typical day-to-day tasks. "
                            "Consider different levels of abstractions. "
                            "Examples include: clicking a button, opening "
                            "a window, operating payroll software, generating "
                            " invoices, renting an apartment.")},
            {"role":"user",
                "content":levelprompt}
        ]
    )

    tasks_by_level[levels[i]] = completion_2.choices[0].message['content']



for level in tasks_by_level:
    print("This is the level {}".format(level))
    print(tasks_by_level[level])
