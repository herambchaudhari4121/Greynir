"""

    Reynir: Natural language processing for Icelandic

    Distance query response module

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


    This module handles distance-related queries.

"""

# TODO: This module should probably use grammar instead of regexes

import re
import logging
import math

from reynir.bindb import BIN_Db
from queries import gen_answer, query_geocode_API_addr
from geo import distance


_DISTANCE_QTYPE = "Distance"


_QREGEXES = (
    r"^hvað er ég langt frá (.+)$",
    r"^hvað er ég langt í burtu frá (.+)$",
    r"^hversu langt er ég frá (.+)$",
    r"^hve langt er ég frá (.+)$",
    r"^hvað er langt á (.+)$",
    r"^hvað er langt í (.+)$",
    # TODO: Fix response for these two (transform location to genitive)
    # r"^hvað er langt til (.+)$",
    # r"^hversu langt er til (.+)$",
)


def _addr2nom(address):
    """ Convert location name to nominative form """
    words = address.split()
    nf = []
    for w in words:
        bin_res = BIN_Db().lookup_nominative(w)
        if not bin_res and not w.islower():
            # Try lowercase form
            bin_res = BIN_Db().lookup_nominative(w.lower())
        if bin_res:
            nf.append(bin_res[0].ordmynd)
        else:
            nf.append(w)
    return " ".join(nf)


def answer_for_remote_loc(locname, query):
    """ Generate response to distance query """
    if not query.location:
        return gen_answer("Ég veit ekki hvar þú ert.")

    loc_nf = _addr2nom(locname[:1].upper() + locname[1:])
    res = query_geocode_API_addr(loc_nf)

    # Verify sanity of API response
    if (
        not res
        or not "status" in res
        or res["status"] != "OK"
        or not res.get("results")
    ):
        return None

    # Extract location coordinates from API result
    topres = res["results"][0]
    coords = topres["geometry"]["location"]
    loc = (coords["lat"], coords["lng"])

    # Calculate distance, round it intelligently and format num string
    km_dist = distance(query.location, loc)
    km_dist = round(km_dist, 1 if km_dist < 10 else 0)

    if km_dist >= 1.0:
        dist = str(km_dist).replace(".", ",")
        dist = re.sub(r",0$", "", dist)
        unit = "kílómetra"
        unit_abbr = "km"
    else:
        dist = int(math.ceil((km_dist * 1000.0) / 10.0) * 10)  # Round to nearest 10 m
        unit = "metra"
        unit_abbr = "m"    

    # Generate answer
    answer = "{0} {1}".format(dist, unit_abbr)
    response = dict(answer=answer)
    loc_nf = loc_nf[0].upper() + loc_nf[1:]
    voice = "{2} er {0} {1} í burtu".format(dist, unit, loc_nf)

    query.set_key(loc_nf)

    return response, answer, voice


def handle_plain_text(q):
    """ Handle a plain text query, contained in the q parameter """
    ql = q.query_lower.rstrip("?")

    remote_loc = None
    for rx in _QREGEXES:
        m = re.search(rx, ql)
        if m:
            remote_loc = m.group(1)
            break
    else:
        return False

    try:
        answ = answer_for_remote_loc(remote_loc, q)
    except Exception as e:
        logging.warning("Exception looking up addr in geocode API: {0}".format(e))
        q.set_error("E_EXCEPTION: {0}".format(e))
        answ = None
    
    if not answ:
        return False

    q.set_qtype(_DISTANCE_QTYPE)
    q.set_answer(*answ)

    return True