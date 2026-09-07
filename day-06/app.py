"""Flask interface for the Day 06 configurable first agent."""

import bleach
import markdown
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent import AgentConfigurationError, AgentError, AgentInputError, AssistantAgent


ALLOWED_MARKDOWN_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"]
load_dotenv()
app = Flask(__name__)
agent = AssistantAgent()


def render_markdown(answer):
    return bleach.clean(markdown.markdown(answer, extensions=["extra", "sane_lists"]), tags=ALLOWED_MARKDOWN_TAGS, attributes={}, strip=True)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/ask")
def ask():
    payload = request.get_json(silent=True)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    controls = payload.get("controls") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify(error="Введите непустой текст вопроса."), 400
    try:
        result = agent.reply(prompt, controls).to_dict()
        result["answer_html"] = render_markdown(result.pop("answer"))
        return jsonify(result)
    except AgentInputError as error:
        return jsonify(error=str(error)), 400
    except AgentConfigurationError as error:
        return jsonify(error=str(error)), 503
    except AgentError as error:
        app.logger.exception("AssistantAgent failed to answer")
        return jsonify(error=str(error)), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5005, debug=True)
