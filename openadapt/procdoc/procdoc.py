"""Automated process documentation."""


from pprint import pformat

from loguru import logger
import cohere

from openadapt.config import config
from openadapt.db import crud
from openadapt.strategies.visual import (
    add_active_segment_descriptions,
    get_action_prompt_dict,
    get_window_prompt_dict,
)
from openadapt import adapters, cache, models, utils


API_KEY = config.COHERE_API_KEY


@cache.cache()
def get_action_description(action: models.ActionEvent):
    system_prompt = utils.render_template_from_file(
        "openadapt/procdoc/system.j2",
    )
    action_dict = get_action_prompt_dict(action)
    try:
        del action_dict["available_segment_descriptions"]
    except:
        pass
    window_dict = get_window_prompt_dict(action.window_event)
    action_description_prompt = utils.render_template_from_file(
        "openadapt/procdoc/describe_action.j2",
        action=action_dict,
        window=window_dict,
    )
    logger.info(f"action_description_prompt=\n{action_description_prompt}")
    if 0:
        prompt_adapter = adapters.get_default_prompt_adapter()
    else:
        prompt_adapter = adapters.cohere
    action_description = prompt_adapter.prompt(
        action_description_prompt,
        system_prompt,
    )
    logger.info(f"action_description=\n{action_description}")
    return action_description


@cache.cache()
def get_process_description(recording: models.Recording):
    system_prompt = utils.render_template_from_file(
        "openadapt/procdoc/system.j2",
    )
    add_active_segment_descriptions(
        recording.processed_action_events,
    )
    actions = recording.processed_action_events
    action_descriptions = [
        get_action_description(action)
        for action in actions
    ]
    process_description_prompt = utils.render_template_from_file(
        "openadapt/procdoc/describe_process.j2",
        action_descriptions=action_descriptions,
    )
    if 0:
        prompt_adapter = adapters.get_default_prompt_adapter()
    else:
        prompt_adapter = adapters.cohere
    process_description = prompt_adapter.prompt(
        process_description_prompt,
        system_prompt,
    )
    logger.info(f"process_description=\n{process_description}")
    return process_description


def vanilla_rag(process_descriptions: list[str]):

    # Step 1 - Indexing and given a user query, retrieve the relevant chunks from the index
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=512,
        chunk_overlap=50,
        length_function=len,
        is_separator_regex=False,
    )

    # Split the text into chunks with some overlap
    chunks_ = text_splitter.create_documents([text])
    chunks = [c.page_content for c in chunks_]
    print(f"The text has been broken down in {len(chunks)} chunks.")

    model="embed-english-v3.0"
    response = co.embed(
        texts= chunks,
        model=model,
        input_type="search_document",
        embedding_types=['float']
    )
    embeddings = response.embeddings.float
    print(f"We just computed {len(embeddings)} embeddings.")

    vector_database = {i: np.array(embedding) for i, embedding in enumerate(embeddings)}

    query = "How do I use the calculator?"
    response = co.embed(
        texts=[query],
        model=model,
        input_type="search_query",
        embedding_types=['float']
    )
    query_embedding = response.embeddings.float[0]
    print("query_embedding: ", query_embedding)

    def cosine_similarity(a, b):
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    # Calculate similarity between the user question & each chunk
    similarities = [cosine_similarity(query_embedding, chunk) for chunk in embeddings]
    print("similarity scores: ", similarities)

    # Get indices of the top 10 most similar chunks
    sorted_indices = np.argsort(similarities)[::-1]

    # Keep only the top 10 indices
    top_indices = sorted_indices[:10]
    print("Here are the indices of the top 10 chunks after retrieval: ", top_indices)

    # Retrieve the top 10 most similar chunks
    top_chunks_after_retrieval = [chunks[i] for i in top_indices]
    print("Here are the top 10 chunks after retrieval: ")
    for t in top_chunks_after_retrieval:
        print("== " + t)


    # Step 2 - Rerank the chunks retrieved from the vector database
    response = co.rerank(
        query=query,
        documents=top_chunks_after_retrieval,
        top_n=3,
        model="rerank-english-v2.0",
    )

    top_chunks_after_rerank = [result.document['text'] for result in response]
    print("Here are the top 3 chunks after rerank: ")
    for t in top_chunks_after_rerank:
        print("== " + t)


    # Step 3 - Generate the model final answer, given the retrieved and reranked chunks
    # preamble containing instructions about the task and the desired style for the output.
    preamble = """
## Task & Context
You help people answer their questions and other requests interactively. You will be asked a very wide array of requests on all kinds of topics. You will be equipped with a wide range of search engines or similar tools to help you, which you use to research your answer. You should focus on serving the user's needs as best you can, which will be wide-ranging.

## Style Guide
Unless the user asks for a different style of answer, you should answer in full sentences, using proper grammar and spelling.
"""
    # retrieved documents
    documents = [
        {"title": "chunk 0", "snippet": top_chunks_after_rerank[0]},
        {"title": "chunk 1", "snippet": top_chunks_after_rerank[1]},
        {"title": "chunk 2", "snippet": top_chunks_after_rerank[2]},
      ]

    # get model response
    response = co.chat(
      message=query,
      documents=documents,
      preamble=preamble,
      model="command-r",
      temperature=0.3
    )

    print("Final answer:")
    print(response.text)


def main():
    MAX_RECORDINGS = 1
    recordings = crud.get_all_recordings()[:MAX_RECORDINGS]
    process_descriptions = [
        get_process_description(recording)
        for recording in recordings
    ]
    logger.info(f"process_descriptions=\n{pformat(process_descriptions)}")
    import ipdb; ipdb.set_trace()

    vanilla_rag(process_descriptions)
    import ipdb; ipdb.set_trace()


if __name__ == "__main__":
    main()
