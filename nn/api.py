#!/usr/bin/env python3
"""
    Greynir: Natural language processing for Icelandic

    Copyright (C) 2020 Miðeind ehf.

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
"""

from quart import request
#  from flask_cors import cross_origin

from routes import (
    routes,
    better_jsonify,
    text_from_request,
    bool_from_request,
    restricted,
)
from nn.nnclient import ParsingClient, TranslateClient


@routes.route("/nnparse.api", methods=["GET", "POST"])
@routes.route("/nnparse.api/v<int:version>", methods=["GET", "POST"])
async def nnparse_api(version=1):
    """ Analyze text manually entered by the user, by passing the request
        to a neural network server and returning its result back """
    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        text = text_from_request(request)  # FIXME!
    except Exception:
        return better_jsonify(valid=False, reason="Invalid request")

    results = ParsingClient.request_sentence(text)
    if results is None:
        return better_jsonify(valid=False, reason="Service unavailable")
    nnTree = results["outputs"]
    scores = results["scores"]
    result = {
        "tree": nnTree.to_dict(),
        "width": nnTree.width(),
        "height": nnTree.height(),
        "scores": scores,
    }

    return better_jsonify(valid=True, result=result)


@routes.route("/nntranslate.api", methods=["GET", "POST"])
@routes.route("/nntranslate.api/v<int:version>", methods=["GET", "POST"])
async def nntranslate_api(version=1):
    """ Translate text manually entered by the user, by passing the request
        to a neural network server and returning its result back """
    if not (1 <= version <= 1):
        return better_jsonify(valid=False, reason="Unsupported version")

    try:
        segmented = True
        trnsl_src = None
        if "application/json" in request.headers["Content-Type"]:
            js = await request.get_json()
            trnsl_src = js["pgs"]
            src_lang = js["src_lang"]
            tgt_lang = js["tgt_lang"]
        else:
            vals = await request.values
            segmented = False
            trnsl_src = text_from_request(request)  # FIXME!
            src_lang = vals.get("src_lang")
            tgt_lang = vals.get("tgt_lang")
        if not trnsl_src:
            return better_jsonify(valid=False, reason="Invalid request")
    except Exception:
        return better_jsonify(valid=False, reason="Invalid request")

    if segmented:
        result = TranslateClient.request_segmented(trnsl_src, src_lang, tgt_lang)
    else:
        result = TranslateClient.request_text(trnsl_src, src_lang, tgt_lang)
    return better_jsonify(valid=True, result=result)


@routes.route("/nn/translate.api", methods=["GET", "POST"])
@routes.route("/nn/translate.api/v<int:version>", methods=["GET", "POST"])
#  @cross_origin()
async def translate_api(version=1):
    from nn.client import TranslationApiClient

    tc = TranslationApiClient()
    return tc.dispatch(request)
