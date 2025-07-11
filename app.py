from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import logging
import os
import requests
import spacy
import networkx as nx
import json
from dotenv import load_dotenv
import textstat
import openai
import time
import io
import re
from elevenlabs import ElevenLabs, VoiceSettings
import tempfile

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load NLP Model
nlp = spacy.load("en_core_web_sm")
graph = nx.DiGraph()  # Mind Map Graph

# ElevenLabs Configuration
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
# More expressive voices
VOICES = {
    "encouraging_female": "ZT9u07TYPVl83ejeLakq",  # Bella - expressive                
}
DEFAULT_VOICE = "encouraging_female"
openai.api_key = os.getenv("OPENAI_API_KEY")

# API Endpoints
CONCEPTNET_API = "https://api.conceptnet.io/query?node=/c/en/{}&rel=/r/RelatedTo&limit=20"
DBPEDIA_API = "http://dbpedia.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# 🔥 Expanded Concept Relations Dataset
concept_relations = {}

MAX_ATTEMPTS = 5
SUMMARY_MAX_WORDS = 120  # Increased token limit
READABILITY_THRESHOLD = 85
MAX_TEXT_LENGTH = 1000  # ElevenLabs character limit

# ... (rest of helper functions remain unchanged) ...
def fetch_conceptnet_relations(concept):
    url = CONCEPTNET_API.format(concept.replace(" ", "_").lower())
    response = requests.get(url).json()
    related = set()
    for edge in response.get("edges", []):
        end_node = edge["end"]["label"]
        related.add(end_node)
        graph.add_edge(concept, end_node)
    return list(related)

def fetch_dbpedia_relations(concept):
    query = f"""
    SELECT ?related WHERE {{
        <http://dbpedia.org/resource/{concept.replace(' ', '_')}> dbo:wikiPageWikiLink ?related .
    }} LIMIT 20
    """
    params = {"query": query, "format": "json"}
    response = requests.get(DBPEDIA_API, params=params).json()
    related = set()
    for result in response.get("results", {}).get("bindings", []):
        related_concept = result["related"]["value"].split("/")[-1].replace("_", " ")
        related.add(related_concept)
        graph.add_edge(concept, related_concept)
    return list(related)

def fetch_wikidata_relations(concept):
    params = {
        "action": "wbsearchentities",
        "search": concept,
        "language": "en",
        "format": "json"
    }
    response = requests.get(WIKIDATA_API, params=params).json()
    related = set()
    for entity in response.get("search", []):
        related.add(entity["label"])
        graph.add_edge(concept, entity["label"])
    return list(related)

def extract_textual_concepts(text):
    doc = nlp(text)
    return list(set([ent.text for ent in doc.ents]))

def expand_concept_dataset(concept):
    if concept in concept_relations:
        return concept_relations[concept]
    related_concepts = fetch_conceptnet_relations(concept) + fetch_dbpedia_relations(concept) + fetch_wikidata_relations(concept)
    parent = concept
    structured_relations = {parent: []}
    for child in related_concepts:
        structured_relations[parent].append(child)
        graph.add_edge(parent, child)
    concept_relations[concept] = structured_relations
    return structured_relations


def complete_sentence(text):
    """Ensure the summary ends with a complete sentence"""
    if not text:
        return text
    
    # Check if text ends with proper punctuation
    if re.search(r'[.!?]$', text):
        return text
    
    # Find last punctuation mark
    last_punct = max(text.rfind('.'), text.rfind('!'), text.rfind('?'))
    if last_punct > 0:
        return text[:last_punct + 1]
    
    # If no punctuation, add period
    return text + '.'

# Summarization parameters
@app.route('/api/summarize', methods=['POST'])
def summarize():
    """GPT-4o Summarization optimized for dyslexic users"""
    data = request.get_json()
    input_text = data.get("text", "").strip()

    if not input_text:
        return jsonify({"error": "No text provided."}), 400

    try:
        # Truncate very long text to avoid token limits
        if len(input_text) > 10000:
            input_text = input_text[:10000] + " [TEXT TRUNCATED]"
        
        prompt_template = (
            "You are a helpful assistant. Summarize the article below in a way that is very easy to read. "
            "Use ultra-short, simple sentences. Use words suitable for someone with dyslexia. "
            "Avoid difficult vocabulary or long paragraphs. Make sure to complete all sentences.\n\n"
            "Example:\n"
            "Text: Scientists discovered a new planet that might support life.\n"
            "Summary: A new planet was found. It may have life.\n\n"
            f"Now summarize this article:\n{input_text}\n\nSummary:"
        )

        # Try generating a readable summary within MAX_ATTEMPTS
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt_template}],
                    temperature=0.5,
                    max_tokens=SUMMARY_MAX_WORDS
                )
                summary = response.choices[0].message.content.strip()
                # Ensure sentence completion
                summary = complete_sentence(summary)
                readability = textstat.flesch_reading_ease(summary)

                if readability >= READABILITY_THRESHOLD or attempt == MAX_ATTEMPTS - 1:
                    return jsonify({
                        "summary_text": summary,
                        "readability": readability,
                        "attempts": attempt + 1
                    })

                time.sleep(1)

            except openai.error.OpenAIError as e:
                logger.error(f"OpenAI API error: {str(e)}")
                return jsonify({"error": f"OpenAI error: {str(e)}"}), 500
            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
                return jsonify({"error": "Internal server error"}), 500

        return jsonify({"error": "Failed to generate readable summary"}), 500
    except Exception as e:
        logger.exception("Unexpected error in summarization")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/synthesize", methods=["POST"])
def synthesize():
    

    data = request.get_json()
    text = data.get("text", "").strip()
    voice_type = data.get("voice", DEFAULT_VOICE)
    
    if not text:
        return jsonify({"error": "No text provided"}), 400
    
    if not ELEVENLABS_API_KEY:
        logger.error("ElevenLabs API key not configured")
        return jsonify({"error": "Server configuration error"}), 500
    
    try:
        voice_id = VOICES.get(voice_type, VOICES[DEFAULT_VOICE])

        if len(text) > MAX_TEXT_LENGTH:
            logger.warning(f"Truncating text from {len(text)} to {MAX_TEXT_LENGTH} characters")
            text = text[:MAX_TEXT_LENGTH]
        
        logger.info(f"Synthesizing with voice: {voice_type} ({voice_id})")

        # ElevenLabs Client
        client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

        # Match style from FastAPI version
        voice_settings = VoiceSettings(
            stability=0.75,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=True
        )

        # Generate audio
        audio_stream = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_turbo_v2_5",
            output_format="mp3_44100_128",
            voice_settings=voice_settings
        )

        # Save to temp file
        audio_bytes = b"".join(audio_stream)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        temp_file.write(audio_bytes)
        temp_file.close()

        return send_file(
            temp_file.name,
            mimetype='audio/mpeg',
            as_attachment=False
        )

    except Exception as e:
        logger.exception("TTS Error")
        return jsonify({"error": f"TTS Failed: {str(e)}"}), 500

# ... (rest of the endpoints remain unchanged) ...

@app.route('/api/related-concepts', methods=['POST'])
def related_concepts():
    try:
        data = request.get_json()
        concept = data.get('concept', '').strip()
        if not concept:
            return jsonify({'error': 'No concept provided'}), 400
        
        expanded_relations = expand_concept_dataset(concept)
        return jsonify({
            'concept': concept,
            'related_concepts': list(expanded_relations.get(concept, []))[:10] 
        })
    except Exception as e:
        logger.error(f"Related concepts error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/mindmap', methods=['GET'])
def get_mindmap():
    try:
        nodes = [{"id": node} for node in graph.nodes]
        edges = [{"source": source, "target": target} for source, target in graph.edges]
        return jsonify({"nodes": nodes, "edges": edges})
    except Exception as e:
        logger.error(f"Mindmap error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    app.run(debug=True)