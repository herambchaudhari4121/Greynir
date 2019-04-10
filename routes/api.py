"""

    Reynir: Natural language processing for Icelandic

    Copyright (C) 2019 Miðeind ehf.

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    API routes
    Note: All routes ending with .api are configured not to be cached by nginx

"""


from . import routes, better_jsonify, text_from_request, bool_from_request
from . import _MAX_URL_LENGTH, _MAX_UUID_LENGTH
from flask import request, abort, current_app
from tnttagger import ifd_tag
from db import SessionContext
from db.models import ArticleTopic
from treeutil import TreeUtility
from correct import check_grammar
from reynir.binparser import canonicalize_token
from reynir import correct_spaces, tokenize
from article import Article as ArticleProxy
from nertokenizer import recognize_entities
from query import Query
from images import get_image_url
import logging


@routes.route("/ifdtag.api", methods=["GET", "POST"])
@routes.route("/ifdtag.api/v<int:version>", methods=["GET", "POST"])
def ifdtag_api(version=1):
    """ API to parse text and return IFD tagged tokens in a simple and sparse JSON format """
    if not (1 <= version <= 1):
        # Unsupported version
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        text = text_from_request(request)
    except:
        return better_jsonify(valid=False, reason="Invalid request")

    pgs = ifd_tag(text)

    return better_jsonify(valid=bool(pgs), result=pgs)


@routes.route("/analyze.api", methods=["GET", "POST"])
@routes.route("/analyze.api/v<int:version>", methods=["GET", "POST"])
def analyze_api(version=1):
    """ Analyze text manually entered by the user, i.e. not coming from an article.
        This is a lower level API used by the Greynir web front-end. """
    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")
    # try:
    text = text_from_request(request)
    # except:
    #     return better_jsonify(valid=False, reason="Invalid request")
    with SessionContext(commit=True) as session:
        pgs, stats, register = TreeUtility.tag_text(session, text, all_names=True)
    # Return the tokens as a JSON structure to the client
    return better_jsonify(valid=True, result=pgs, stats=stats, register=register)


@routes.route("/correct.api", methods=["GET", "POST"])
@routes.route("/correct.api/v<int:version>", methods=["GET", "POST"])
def correct_api(version=1):
    """ Correct text manually entered by the user, i.e. not coming from an article.
        This is a lower level API used by the Greynir web front-end. """
    if current_app.config["PRODUCTION"]:
        return abort(403) # Forbidden

    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        text = text_from_request(request)
    except Exception as e:
        logging.warning("Exception in correct_api(): {0}".format(e))
        return better_jsonify(valid=False, reason="Invalid request")

    pgs, stats = check_grammar(text)

    # Return the annotated paragraphs/sentences and stats
    # in a JSON structure to the client
    return better_jsonify(valid=True, result=pgs, stats=stats)


@routes.route("/postag.api", methods=["GET", "POST"])
@routes.route("/postag.api/v<int:version>", methods=["GET", "POST"])
def postag_api(version=1):
    """ API to parse text and return POS tagged tokens in a verbose JSON format """
    if not (1 <= version <= 1):
        # Unsupported version
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        text = text_from_request(request)
    except:
        return better_jsonify(valid=False, reason="Invalid request")

    with SessionContext(commit=True) as session:
        pgs, stats, register = TreeUtility.tag_text(session, text, all_names=True)
        # Amalgamate the result into a single list of sentences
        if pgs:
            # Only process the first paragraph, if there are many of them
            if len(pgs) == 1:
                pgs = pgs[0]
            else:
                # More than one paragraph: gotta concatenate 'em all
                pa = []
                for pg in pgs:
                    pa.extend(pg)
                pgs = pa
        for sent in pgs:
            # Transform the token representation into a
            # nice canonical form for outside consumption
            # err = any("err" in t for t in sent)
            for t in sent:
                canonicalize_token(t)

    # Return the tokens as a JSON structure to the client
    return better_jsonify(valid=True, result=pgs, stats=stats, register=register)


@routes.route("/parse.api", methods=["GET", "POST"])
@routes.route("/parse.api/v<int:version>", methods=["GET", "POST"])
def parse_api(version=1):
    """ API to parse text and return POS tagged tokens in JSON format """
    if not (1 <= version <= 1):
        # Unsupported version
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        text = text_from_request(request)
    except:
        return better_jsonify(valid=False, reason="Invalid request")

    with SessionContext(commit=True) as session:
        pgs, stats, register = TreeUtility.parse_text(session, text, all_names=True)
        # In this case, we should always get a single paragraph back
        if pgs:
            # Only process the first paragraph, if there are many of them
            if len(pgs) == 1:
                pgs = pgs[0]
            else:
                # More than one paragraph: gotta concatenate 'em all
                pa = []
                for pg in pgs:
                    pa.extend(pg)
                pgs = pa

    # Return the tokens as a JSON structure to the client
    return better_jsonify(valid=True, result=pgs, stats=stats, register=register)


@routes.route("/article.api", methods=["GET", "POST"])
@routes.route("/article.api/v<int:version>", methods=["GET", "POST"])
def article_api(version=1):
    """ Obtain information about an article, given its URL or id """

    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")

    if request.method == "GET":
        url = request.args.get("url")
        uuid = request.args.get("id")
    else:
        url = request.form.get("url")
        uuid = request.form.get("id")
    if url:
        url = url.strip()[0:_MAX_URL_LENGTH]
    if uuid:
        uuid = uuid.strip()[0:_MAX_UUID_LENGTH]
    if url:
        # URL has priority, if both are specified
        uuid = None
    if not url and not uuid:
        return better_jsonify(valid=False, reason="No url or id specified in query")

    with SessionContext(commit=True) as session:

        if uuid:
            a = ArticleProxy.load_from_uuid(uuid, session)
        elif url.startswith("http:") or url.startswith("https:"):
            a = ArticleProxy.load_from_url(url, session)
        else:
            a = None

        if a is None:
            return better_jsonify(valid=False, reason="Article not found")

        if a.html is None:
            return better_jsonify(valid=False, reason="Unable to fetch article")

        # Prepare the article for display
        a.prepare(session)
        register = a.create_register(session, all_names=True)
        # Fetch names of article topics, if any
        topics = (
            session.query(ArticleTopic).filter(ArticleTopic.article_id == a.uuid).all()
        )
        topics = [dict(name=t.topic.name, id=t.topic.identifier) for t in topics]

    return better_jsonify(
        valid=True,
        url=a.url,
        id=a.uuid,
        heading=a.heading,
        author=a.author,
        ts=a.timestamp.isoformat()[0:19],
        num_sentences=a.num_sentences,
        num_parsed=a.num_parsed,
        ambiguity=a.ambiguity,
        register=register,
        topics=topics,
    )


@routes.route("/reparse.api", methods=["POST"])
@routes.route("/reparse.api/v<int:version>", methods=["POST"])
def reparse_api(version=1):
    """ Reparse an already parsed and stored article with a given UUID """
    if not (1 <= version <= 1):
        return better_jsonify(valid="False", reason="Unsupported version")

    uuid = request.form.get("id", "").strip()[0:_MAX_UUID_LENGTH]
    tokens = None
    register = {}
    stats = {}

    with SessionContext(commit=True) as session:
        # Load the article
        a = ArticleProxy.load_from_uuid(uuid, session)
        if a is not None:
            # Found: Parse it (with a fresh parser) and store the updated version
            a.parse(session, verbose=True, reload_parser=True)
            # Save the tokens
            tokens = a.tokens
            # Build register of person names
            register = a.create_register(session)
            stats = dict(
                num_tokens=a.num_tokens,
                num_sentences=a.num_sentences,
                num_parsed=a.num_parsed,
                ambiguity=a.ambiguity,
            )

    # Return the tokens as a JSON structure to the client,
    # along with a name register and article statistics
    return better_jsonify(valid=True, result=tokens, register=register, stats=stats)


# Frivolous fun stuff

_SPECIAL_QUERIES = {
    "er þetta spurning?": {"answer": "Er þetta svar?"},
    "er þetta svar?": {"answer": "Er þetta spurning?"},
    "hvað er svarið?": {"answer": "42."},
    "hvert er svarið?": {"answer": "42."},
    "veistu allt?": {"answer": "Nei."},
    "hvað veistu?": {"answer": "Spurðu mig!"},
    "veistu svarið?": {"answer": "Spurðu mig!"},
    "hvað heitir þú?": {"answer": "Greynir. Ég er grey sem reynir að greina íslensku."},
    "hver ert þú?": {"answer": "Ég er grey sem reynir að greina íslensku."},
    "hver bjó þig til?": {"answer": "Villi."},
    "hver skapaði þig?": {"answer": "Villi."},
    "hver er skapari þinn?": {"answer": "Villi."},
    "hver er flottastur?": {"answer": "Villi."},
    "hver er ég?": {"answer": "Þú ert þú."},
    "hvar er ég?": {"answer": "Þú ert hérna."},
    "er guð til?": {"answer": "Ég held ekki."},
    "hver skapaði guð?": {"answer": "Enginn sem ég þekki."},
    "hver skapaði heiminn?": {"answer": "Enginn sem ég þekki."},
    "hver er tilgangur lífsins?": {"answer": "42."},
    "hvar endar alheimurinn?": {"answer": "Inni í þér."},
}

_MAX_QUERY_LENGTH = 512


def process_query(session, toklist, result):
    """ Check whether the parse tree is describes a query, and if so, execute the query,
        store the query answer in the result dictionary and return True """
    q = Query(session)
    if not q.parse(toklist, result):
        # if Settings.DEBUG:
        #     print("Unable to parse query, error {0}".format(q.error()))
        result["error"] = q.error()
        return False
    if not q.execute():
        # This is a query, but its execution failed for some reason: return the error
        # if Settings.DEBUG:
        #     print("Unable to execute query, error {0}".format(q.error()))
        result["error"] = q.error()
        return True
    # Successful query: return the answer in response
    result["response"] = q.answer()
    # ...and the query type, as a string ('Person', 'Entity', 'Title' etc.)
    result["qtype"] = qt = q.qtype()
    result["key"] = q.key()
    if qt == "Person":
        # For a person query, add an image (if available)
        img = get_image_url(q.key(), enclosing_session=session)
        if img is not None:
            result["image"] = dict(
                src=img.src,
                width=img.width,
                height=img.height,
                link=img.link,
                origin=img.origin,
                name=img.name,
            )
    return True


@routes.route("/query.api", methods=["GET", "POST"])
@routes.route("/query.api/v<int:version>", methods=["GET", "POST"])
def query_api(version=1):
    """ Respond to a query string """

    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")

    if request.method == "GET":
        q = request.args.get("q", "")
    else:
        q = request.form.get("q", "")
    q = q.strip()[0:_MAX_QUERY_LENGTH]

    # Auto-uppercasing can be turned off by sending autouppercase: false in the query JSON
    auto_uppercase = bool_from_request(request, "autouppercase", True)
    result = dict()
    ql = q.lower()

    if ql in _SPECIAL_QUERIES or (ql + "?") in _SPECIAL_QUERIES:
        result["valid"] = True
        result["qtype"] = "Special"
        result["q"] = q
        if ql in _SPECIAL_QUERIES:
            result["response"] = _SPECIAL_QUERIES[ql]
        else:
            result["response"] = _SPECIAL_QUERIES[ql + "?"]
    else:
        with SessionContext(commit=True) as session:

            toklist = tokenize(
                q, auto_uppercase=q.islower() if auto_uppercase else False
            )
            toklist = list(recognize_entities(toklist, enclosing_session=session))
            actual_q = correct_spaces(" ".join(t.txt for t in toklist if t.txt))

            # if Settings.DEBUG:
            #     # Log the query string as seen by the parser
            #     print("Query is: '{0}'".format(actual_q))

            # Try to parse and process as a query
            try:
                is_query = process_query(session, toklist, result)
            except:
                is_query = False

        result["valid"] = is_query
        result["q"] = actual_q

    return better_jsonify(**result)