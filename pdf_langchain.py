import glob
import os
import pathlib
import subprocess
import tempfile
from abc import ABC
from typing import Optional, List, Mapping, Any

import pandas as pd
import requests
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter
from langchain.vectorstores import FAISS
from langchain.chains.question_answering import load_qa_chain
from langchain.chains import ConversationalRetrievalChain
from langchain.docstore.document import Document
from langchain.llms.base import LLM

# https://python.langchain.com/en/latest/modules/models/llms/examples/llm_caching.html
# from langchain.cache import InMemoryCache
# langchain.llm_cache = InMemoryCache()
try:
    raise ValueError("Disabled, too greedy even if change model etc.")
    import langchain
    from langchain.cache import SQLiteCache

    langchain.llm_cache = SQLiteCache(database_path=".langchain.db")
    print("Caching", flush=True)
except Exception as e:
    print("NO caching: %s" % str(e), flush=True)


def get_db(path=None, pdf_filename=None, split_method='chunk', use_openai_embedding=False):
    # get chunks of data to handle model context
    chunks = get_chunks(path=path, pdf_filename=pdf_filename, split_method=split_method)

    # get embedding model
    embedding = get_embedding(use_openai_embedding)

    # Create vector database
    db = FAISS.from_documents(chunks, embedding)

    return db


def get_chunks(path=None, pdf_filename=None, split_method='chunk'):
    if split_method == 'page':
        # Simple method - Split by pages
        loader = PyPDFLoader(pdf_filename)
        pages = loader.load_and_split()
        print(pages[0])

        # SKIP TO STEP 2 IF YOU'RE USING THIS METHOD
        chunks = pages
    elif split_method == 'chunk':
        # Advanced method - Split by chunk

        # Step 1: Convert PDF to text
        import textract
        doc = textract.process(pdf_filename)

        # Step 2: Save to .txt and reopen (helps prevent issues)
        txt_filename = pdf_filename.replace('.pdf', '.txt')
        with open(txt_filename, 'w') as f:
            f.write(doc.decode('utf-8'))

        with open(txt_filename, 'r') as f:
            text = f.read()

        # Step 3: Create function to count tokens
        from transformers import GPT2TokenizerFast
        tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")

        def count_tokens(textin: str) -> int:
            return len(tokenizer.encode(textin))

        # Step 4: Split text into chunks
        text_splitter = RecursiveCharacterTextSplitter(
            # Set a really small chunk size, just to show.
            chunk_size=512,
            chunk_overlap=24,
            length_function=count_tokens,
        )

        chunks = text_splitter.create_documents([text])

        # show_counts(chunks, count_tokens)
    else:
        raise RuntimeError("No such split_method=%s" % split_method)

    # Result is many LangChain 'Documents' around 500 tokens or less (Recursive splitter sometimes allows more tokens to retain context)
    type(chunks[0])
    return chunks


def get_embedding(use_openai_embedding):
    # Get embedding model
    if use_openai_embedding:
        assert os.getenv("OPENAI_API_KEY") is not None, "Set ENV OPENAI_API_KEY"
        from langchain.embeddings import OpenAIEmbeddings
        embedding = OpenAIEmbeddings()
    else:
        from langchain.embeddings import HuggingFaceEmbeddings

        # model_name = "sentence-transformers/all-mpnet-base-v2"  # poor
        model_name = "sentence-transformers/all-MiniLM-L6-v2"  # good, gets authors
        # model_name = "sentence-transformers/all-MiniLM-L12-v2"  # 12 layers FAILS OOM I think
        # model_name = "sentence-transformers/paraphrase-MiniLM-L6-v2"
        # model_name = 'cerebras/Cerebras-GPT-2.7B' # OOM
        # model_name = 'microsoft/deberta-v3-base'  # microsoft/mdeberta-v3-base for multilinguial
        load_8bit = False
        device, torch_dtype, context_class = get_device_dtype()
        model_kwargs = dict(device=device)  # , torch_dtype=torch_dtype, load_in_8bit=load_8bit)
        embedding = HuggingFaceEmbeddings(model_name=model_name, model_kwargs=model_kwargs)

        # for some models need to fix tokenizer
        if model_name in ['cerebras/Cerebras-GPT-2.7B']:
            embedding.client.tokenizer.pad_token = embedding.client.tokenizer.eos_token

        # also see:
        # https://www.sbert.net/docs/pretrained-models/msmarco-v3.html
        # https://www.sbert.net/examples/applications/semantic-search/README.html
        # https://towardsdatascience.com/bert-for-measuring-text-similarity-eec91c6bf9e1
        # https://discuss.huggingface.co/t/get-word-embeddings-from-transformer-model/6929/2
    return embedding


def get_answer_from_sources(chain, sources, question):
    return chain(
        {
            "input_documents": sources,
            "question": question,
        },
        return_only_outputs=True,
    )["output_text"]


def get_llm(use_openai_model=False):
    if use_openai_model:
        from langchain.llms import OpenAI
        llm = OpenAI(temperature=0)
        model_name = 'openai'
    else:
        from transformers import AutoTokenizer, AutoModelForCausalLM

        #model_name = "cerebras/Cerebras-GPT-2.7B"
        # model_name = "cerebras/Cerebras-GPT-13B"
        # model_name = "cerebras/Cerebras-GPT-6.7B"
        model_name = 'h2oai/h2ogpt-oasst1-512-12b'
        #model_name = 'h2oai/h2ogpt-oig-oasst1-512-6.9b'
        #model_name = 'h2oai/h2ogpt-oasst1-512-20b'
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        device, torch_dtype, context_class = get_device_dtype()

        with context_class(device):
            load_8bit = False
            # FIXME: for now not to spread across hetero GPUs
            # device_map={"": 0} if load_8bit and device == 'cuda' else "auto"
            device_map = {"": 0} if device == 'cuda' else "auto"
            model = AutoModelForCausalLM.from_pretrained(model_name,
                                                         device_map=device_map,
                                                         torch_dtype=torch_dtype,
                                                         load_in_8bit=load_8bit)
            if 'h2ogpt' in model_name:
                from h2oai_pipeline import H2OTextGenerationPipeline
                pipe = H2OTextGenerationPipeline(model=model, tokenizer=tokenizer, max_new_tokens=256, return_full_text=True)
                #pipe.task = "text-generation"
                # below makes it listen only to our prompt removal, not built in prompt removal that is less general and not specific for our model
                pipe.task = "text2text-generation"
            else:
                # only for non-instruct tuned cases when ok with just normal next token prediction
                from transformers import pipeline
                pipe = pipeline(
                    "text-generation", model=model, tokenizer=tokenizer,
                    max_new_tokens=256, early_stopping=False,
                )

            from langchain.llms import HuggingFacePipeline
            llm = HuggingFacePipeline(pipeline=pipe)
    return llm, model_name


def get_llm_prompt(model_name):
    from langchain import PromptTemplate

    if 'h2ogpt' in model_name:
        template = """<human>: {question}
<bot>: """
    else:
        template = """
                {question}
                """

    prompt = PromptTemplate(
        input_variables=["question"],
        template=template,
    )
    return prompt


def get_llm_chain(llm, model_name):
    from langchain import LLMChain

    prompt = get_llm_prompt(model_name)

    chain = LLMChain(
        llm=llm,
        verbose=True,
        prompt=prompt
    )
    return chain


def get_answer_from_db(db, query, sources=False, chat_history='', use_openai_model=False, k=4, chat=True):
    # Check similarity search is working
    # docs = db.similarity_search(query, k=k)
    # print(docs[0])

    # get LLM
    llm, model_name = get_llm(use_openai_model=use_openai_model)

    # Create QA chain to integrate similarity search with user queries (answer query from knowledge base)
    if use_openai_model:
        if sources:
            chain = load_qa_with_sources_chain(llm, chain_type="stuff")
        else:
            chain = load_qa_chain(llm, chain_type="stuff")
    else:
        # make custom llm prompt aware
        chain = get_llm_chain(llm, model_name)
        # WIP OPTIONAL
        if False:
            from langchain import SerpAPIWrapper
            from langchain.agents import Tool
            from langchain.agents import initialize_agent

            serpapi = SerpAPIWrapper(serpapi_api_key='...')
            tools = [
                Tool(
                    name="Search",
                    func=serpapi.run,
                    description="useful for when you need to get a weather forecast"
                )
            ]

            agent = initialize_agent(tools, llm, agent="zero-shot-react-description", verbose=True)
            res = agent.run(input="What is the weather forecast for Poznan, Poland")
            print(res)

    docs = db.similarity_search(query, k=k)

    if chat:
        chain.run(input_documents=docs, question=query)
        # Create conversation chain that uses our vectordb as retriever, this also allows for chat history management
        qa = ConversationalRetrievalChain.from_llm(llm, db.as_retriever())

        # [x.page_content for x in docs if 'Illia' in x.page_content]
        result = qa({"question": query, "chat_history": chat_history})
        answer = result['answer']
    else:
        answer = chain(
            {
                "input_documents": docs,
                "question": question,
            },
            return_only_outputs=True,
        )["output_text"]

    return answer


def get_device_dtype():
    # torch.device("cuda") leads to cuda:x cuda:y mismatches for multi-GPU consistently
    import torch
    n_gpus = torch.cuda.device_count() if torch.cuda.is_available else 0
    device = 'cpu' if n_gpus == 0 else 'cuda'
    # from utils import NullContext
    # context_class = NullContext if n_gpus > 1 or n_gpus == 0 else context_class
    context_class = torch.device
    torch_dtype = torch.float16 if device == 'cuda' else torch.float32
    return device, torch_dtype, context_class


class H2OChatBotLLM(LLM, ABC):
    # FIXME: WIP, use gradio_client not requests
    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        response = requests.post(
            "http://0.0.0.0:7860/prompt",
            json={
                "prompt": prompt,
                "temperature": 0,
                "max_new_tokens": 256,
                "stop": stop + ["Observation:"]
            }
        )
        response.raise_for_status()
        return response.json()["response"]

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }


def show_counts(chunks, count_tokens):
    # Quick data visualization to ensure chunking was successful

    # Create a list of token counts
    token_counts = [count_tokens(chunk.page_content) for chunk in chunks]

    # Create a DataFrame from the token counts
    df = pd.DataFrame({'Token Count': token_counts})

    # Create a histogram of the token count distribution
    df.hist(bins=40, )

    # Show the plot
    import matplotlib.pyplot as plt
    plt.show()


def get_wiki_data(title, first_paragraph_only, text_limit=None, take_head=True):
    url = f"https://en.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&explaintext=1&titles={title}"
    if first_paragraph_only:
        url += "&exintro=1"
    filename = 'wiki_%s.data' % title
    import json
    if not os.path.isfile(filename):
        data = requests.get(url).json()
        json.dump(data, open(filename, 'wt'))
    else:
        data = json.load(open(filename, "rt"))
    page_content = list(data["query"]["pages"].values())[0]["extract"]
    if take_head is not None:
        page_content = page_content[:text_limit] if take_head else page_content[:-text_limit]
    return Document(
        page_content=page_content,
        metadata={"source": f"https://en.wikipedia.org/wiki/{title}"},
    )


def get_wiki_sources(first_para=True, text_limit=None):
    return [
        get_wiki_data("Unix", first_para, text_limit=text_limit),
        get_wiki_data("Microsoft_Windows", first_para, text_limit=text_limit),
        get_wiki_data("Linux", first_para, text_limit=text_limit),
        get_wiki_data("Seinfeld", first_para, text_limit=text_limit),
    ]


def get_github_docs(repo_owner, repo_name):
    with tempfile.TemporaryDirectory() as d:
        subprocess.check_call(
            f"git clone --depth 1 https://github.com/{repo_owner}/{repo_name}.git .",
            cwd=d,
            shell=True,
        )
        git_sha = (
            subprocess.check_output("git rev-parse HEAD", shell=True, cwd=d)
                .decode("utf-8")
                .strip()
        )
        repo_path = pathlib.Path(d)
        markdown_files = list(repo_path.glob("*/*.md")) + list(
            repo_path.glob("*/*.mdx")
        )
        for markdown_file in markdown_files:
            with open(markdown_file, "r") as f:
                relative_path = markdown_file.relative_to(repo_path)
                github_url = f"https://github.com/{repo_owner}/{repo_name}/blob/{git_sha}/{relative_path}"
                yield Document(page_content=f.read(), metadata={"source": github_url})


def get_rst_docs(path):
    md_files = glob.glob(os.path.join(path, "./**/*.md"), recursive=True)
    rst_files = glob.glob(os.path.join(path, "./**/*.rst"), recursive=True)
    for file in md_files + rst_files:
        with open(file, "r") as f:
            yield Document(page_content=f.read(), metadata={"source": file})


def test_qa_wiki_openai():
    return run_qa_wiki(use_openai_model=True)


def test_qa_wiki_stuff_hf():
    # NOTE: total context length makes things fail when n_sources * text_limit >~ 2048
    return run_qa_wiki(use_openai_model=False, text_limit=256, chain_type='stuff')


def test_qa_wiki_map_reduce_hf():
    return run_qa_wiki(use_openai_model=False, text_limit=None, chain_type='map_reduce')


def run_qa_wiki(use_openai_model=False, first_para=True, text_limit=None, chain_type='stuff'):
    sources = get_wiki_sources(first_para=first_para, text_limit=text_limit)
    llm, model_name = get_llm(use_openai_model=use_openai_model)
    chain = load_qa_with_sources_chain(llm, chain_type=chain_type)

    question = "What are the main differences between Linux and Windows?"
    answer = get_answer_from_sources(chain, sources, question)
    print(answer)


def test_qa_wiki_db_openai():
    return run_qa_db(use_openai_model=True, use_openai_embedding=True, text_limit=None, wiki=True)


def test_qa_wiki_db_hf():
    # if don't chunk, still need to limit
    # but this case can handle at least more documents, by picking top k
    # FIXME: but spitting out garbage answer right now, all fragmented, or just 1-word answer
    return run_qa_db(use_openai_model=False, use_openai_embedding=False, text_limit=256, wiki=True)


def test_qa_wiki_db_chunk_hf():
    return run_qa_db(use_openai_model=False, use_openai_embedding=False, text_limit=256, chunk=True, chunk_size=256, wiki=True)


def test_qa_wiki_db_chunk_openai():
    # don't need 256, just seeing how compares to hf
    return run_qa_db(use_openai_model=True, use_openai_embedding=True, text_limit=256, chunk=True, chunk_size=256, wiki=True)


def test_qa_github_db_chunk_openai():
    # don't need 256, just seeing how compares to hf
    query = "what is a software defined asset"
    return run_qa_db(query=query, use_openai_model=True, use_openai_embedding=True, text_limit=256, chunk=True, chunk_size=256, github=True)


def test_qa_daidocs_db_chunk_hf():
    # FIXME: doesn't work well with non-instruct-tuned Cerebras
    query = "Which config.toml enables pytorch for NLP?"
    return run_qa_db(query=query, use_openai_model=False, use_openai_embedding=False, text_limit=None, chunk=True, chunk_size=128, wiki=False,
                     dai_rst=True)


def test_qa_daidocs_db_chunk_openai():
    query = "Which config.toml enables pytorch for NLP?"
    return run_qa_db(query=query, use_openai_model=True, use_openai_embedding=True, text_limit=256, chunk=True, chunk_size=256, wiki=False, dai_rst=True)


def test_qa_daidocs_db_chunk_openaiembedding_hfmodel():
    query = "Which config.toml enables pytorch for NLP?"
    return run_qa_db(query=query, use_openai_model=False, use_openai_embedding=True, text_limit=None, chunk=True, chunk_size=128, wiki=False, dai_rst=True)


def run_qa_db(query=None, use_openai_model=False, use_openai_embedding=False,
              first_para=True, text_limit=None, k=4, chunk=False, chunk_size=1024,
              wiki=False, github=False, dai_rst=False):
    # see https://dagster.io/blog/chatgpt-langchain
    if wiki:
        sources = get_wiki_sources(first_para=first_para, text_limit=text_limit)
    elif github:
        # sources = get_github_docs("dagster-io", "dagster")
        sources = get_github_docs("h2oai", "h2ogpt")
    elif dai_rst:
        home = os.path.expanduser('~')
        sources = get_rst_docs(os.path.join(home, "h2oai.superclean/docs/"))
    else:
        raise RuntimeError("No input data set")
    assert sources, "No sources"

    if chunk:
        # allows handling full docs if passed first_para=False
        source_chunks = []
        # NLTK and SPACY can be used instead
        splitter = CharacterTextSplitter(separator=" ", chunk_size=chunk_size, chunk_overlap=0)
        for source in sources:
            for chunky in splitter.split_text(source.page_content):
                source_chunks.append(Document(page_content=chunky, metadata=source.metadata))
        sources = source_chunks

    llm, model_name = get_llm(use_openai_model=use_openai_model)
    #llm = get_llm_chain(llm, model_name)
    #prompt = get_llm_prompt(model_name)
    embedding = get_embedding(use_openai_embedding)
    db = FAISS.from_documents(sources, embedding)
    chain = load_qa_with_sources_chain(llm)

    if query is None:
        query = "What are the main differences between Linux and Windows?"
    docs = db.similarity_search(query, k=k)

    answer = chain(
        {
            "input_documents": docs,
            "question": query,
        },
        return_only_outputs=True,
    )["output_text"]
    print(answer)


def test_demo_openai():
    return run_demo(use_openai_model=True, use_openai_embedding=True)


def test_demo_hf():
    return run_demo(use_openai_model=False, use_openai_embedding=False)


def run_demo(use_openai_model=False, use_openai_embedding=False):
    # quick test
    pdf_filename = '1706.03762.pdf'
    if not os.path.isfile(pdf_filename):
        if os.path.isfile('1706.03762'):
            os.remove('1706.03762')
        os.system("wget --user-agent TryToStopMeFromUsingWgetNow https://arxiv.org/pdf/1706.03762")
        os.rename('1706.03762', '1706.03762.pdf')
    db = get_db(pdf_filename=pdf_filename, split_method='chunk', use_openai_embedding=use_openai_embedding)
    query = "Who created transformers?"
    answer = get_answer_from_db(db, query, chat_history='', use_openai_model=use_openai_model, k=4)
    print(answer)


if __name__ == '__main__':
    test_demo_hf()