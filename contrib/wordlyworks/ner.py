import spacy


def ner():
    nlp = spacy.load("en_core_web_sm")

    text_description = '''
    Apple Inc. is planning to open a new factory in Austin, Texas. 
    The plan will reportedly cost $1 billion and create jobs for 5,000 people. 
    Apple's CEO, Tim Cook, will oversee the new project.
    '''

    document_object = nlp(text_description)

    for entity in document_object.ents:
        print(f"{entity.text}, ({entity.label_})")


if __name__ == "__main__":
    ner()




