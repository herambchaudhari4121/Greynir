"""
    Reynir: Natural language processing for Icelandic

    Article class

    Copyright (c) 2016 Vilhjalmur Thorsteinsson
    All rights reserved
    See the accompanying README.md file for further licensing and copyright information.

    This module contains a class modeling an article originating from a scraped web page.

"""

import json
import uuid
import time
from datetime import datetime
from collections import OrderedDict, defaultdict, namedtuple

from settings import Settings, NoIndexWords
from scraperdb import Article as ArticleRow, SessionContext, Word, DataError
from fetcher import Fetcher
from tokenizer import TOK, tokenize, canonicalize_token
from fastparser import Fast_Parser, ParseError, ParseForestNavigator, ParseForestDumper
from incparser import IncrementalParser
from query import Query, query_person_title, query_entity_def


WordTuple = namedtuple("WordTuple", ["stem", "cat"])


def add_entity_to_register(name, register, session, all_names = False):
    """ Add the entity name and the 'best' definition to the given name register dictionary.
        If all_names is True, we add all names that occur even if no title is found. """
    if name in register:
        # Already have a definition for this name
        return
    if " " not in name:
        # Single name: this might be the last name of a person/entity
        # that has already been mentioned by full name
        for k in register.keys():
            parts = k.split()
            if len(parts) > 1 and parts[-1] == name:
                # Reference to the last part of a previously defined
                # multi-part person or entity name,
                # for instance 'Clinton' -> 'Hillary Rodham Clinton'
                register[name] = dict(kind = "ref", fullname = k)
                return
    # Use the query module to return definitions for an entity
    definition = query_entity_def(session, name)
    if definition:
        register[name] = dict(kind = "entity", title = definition)
    elif all_names:
        register[name] = dict(kind = "entity", title = None)


def add_name_to_register(name, register, session, all_names = False):
    """ Add the name and the 'best' title to the given name register dictionary """
    if name in register:
        # Already have a title for this name
        return
    # Use the query module to return titles for a person
    title = query_person_title(session, name)
    if title:
        register[name] = dict(kind = "name", title = title)
    elif all_names:
        register[name] = dict(kind = "name", title = None)


def create_name_register(tokens, session, all_names = False):
    """ Assemble a dictionary of person and entity names occurring in the token list """
    register = { }
    for t in tokens:
        if t.kind == TOK.PERSON:
            gn = t.val
            for pn in gn:
                add_name_to_register(pn.name, register, session, all_names = all_names)
        elif t.kind == TOK.ENTITY:
            add_entity_to_register(t.txt, register, session, all_names = all_names)
    return register


class Article:

    _parser = None

    @classmethod
    def _init_class(cls):
        """ Initialize class attributes """
        if cls._parser is None:
            cls._parser = Fast_Parser(verbose = False) # Don't emit diagnostic messages

    @classmethod
    def cleanup(cls):
        if cls._parser is not None:
            cls._parser.cleanup()
            cls._parser = None

    @classmethod
    def get_parser(cls):
        if cls._parser is None:
            cls._init_class()
        return cls._parser

    @classmethod
    def reload_parser(cls):
        """ Force reload of a fresh parser instance """
        cls._parser = None
        cls._init_class()

    @classmethod
    def parser_version(cls):
        """ Return the current grammar timestamp + parser version """
        cls._init_class()
        return cls._parser.version


    def __init__(self, uuid = None, url = None):
        self._uuid = uuid
        self._url = url
        self._heading = ""
        self._author = ""
        self._timestamp = datetime.utcnow()
        self._authority = 1.0
        self._scraped = None
        self._parsed = None
        self._processed = None
        self._indexed = None
        self._scr_module = None
        self._scr_class = None
        self._scr_version = None
        self._parser_version = None
        self._num_tokens = None
        self._num_sentences = 0
        self._num_parsed = 0
        self._ambiguity = 1.0
        self._html = None
        self._tree = None
        self._root_id = None
        self._root_domain = None
        self._helper = None
        self._tokens = None # JSON string
        self._raw_tokens = None # The tokens themselves
        self._words = None # The individual word stems, in a dictionary

    @classmethod
    def _init_from_row(cls, ar):
        """ Initialize a fresh Article instance from a database row object """
        a = cls(uuid = ar.id)
        a._url = ar.url
        a._heading = ar.heading
        a._author = ar.author
        a._timestamp = ar.timestamp
        a._authority = ar.authority
        a._scraped = ar.scraped
        a._parsed = ar.parsed
        a._processed = ar.processed
        a._indexed = ar.indexed
        a._scr_module = ar.scr_module
        a._scr_class = ar.scr_class
        a._scr_version = ar.scr_version
        a._parser_version = ar.parser_version
        assert a._num_tokens is None
        a._num_sentences = ar.num_sentences
        a._num_parsed = ar.num_parsed
        a._ambiguity = ar.ambiguity
        a._html = ar.html
        a._tree = ar.tree
        a._tokens = ar.tokens
        assert a._raw_tokens is None
        a._root_id = ar.root_id
        a._root_domain = ar.root.domain
        return a

    @classmethod
    def _init_from_scrape(cls, url, enclosing_session = None):
        """ Scrape an article from its URL """
        if url is None:
            return None
        a = cls(url = url)
        with SessionContext(enclosing_session) as session:
            # Obtain a helper corresponding to the URL
            html, metadata, helper = Fetcher.fetch_url_html(url, session)
            if html is None:
                return a
            a._html = html
            if metadata is not None:
                a._heading = metadata.heading
                a._author = metadata.author
                a._timestamp = metadata.timestamp
                a._authority = metadata.authority
            a._scraped = datetime.utcnow()
            if helper is not None:
                a._scr_module = helper.scr_module
                a._scr_class = helper.scr_class
                a._scr_version = helper.scr_version
                a._root_id = helper.root_id
                a._root_domain = helper.domain
            return a

    @classmethod
    def load_from_url(cls, url, enclosing_session = None):
        """ Load or scrape an article, given its URL """
        with SessionContext(enclosing_session) as session:
            ar = session.query(ArticleRow).filter(ArticleRow.url == url).one_or_none()
            if ar is not None:
                return cls._init_from_row(ar)
            # Not found in database: attempt to fetch
            return cls._init_from_scrape(url, session)

    @classmethod
    def scrape_from_url(cls, url, enclosing_session = None):
        """ Force fetch of an article, given its URL """
        with SessionContext(enclosing_session) as session:
            ar = session.query(ArticleRow).filter(ArticleRow.url == url).one_or_none()
            a = cls._init_from_scrape(url, session)
            if a is not None and ar is not None:
                # This article already existed in the database, so note its UUID
                a._uuid = ar.id
            return a

    @classmethod
    def load_from_uuid(cls, uuid, enclosing_session = None):
        """ Load an article, given its UUID """
        with SessionContext(enclosing_session) as session:
            try:
                ar = session.query(ArticleRow).filter(ArticleRow.id == uuid).one_or_none()
            except DataError:
                # Probably wrong UUID format
                ar = None
            return None if ar is None else cls._init_from_row(ar)

    @staticmethod
    def _describe_token(t, terminal, meaning):
        """ Return a compact dictionary and a WordTuple describing the token t,
            which matches the given terminal with the given meaning """
        d = dict(x = t.txt)
        wt = None
        if terminal is not None:
            # There is a token-terminal match
            if t.kind == TOK.PUNCTUATION:
                if t.txt == "-":
                    # Hyphen: check whether it is matching an em or en-dash terminal
                    if terminal.cat == "em":
                        d["x"] = "—" # Substitute em dash (will be displayed with surrounding space)
                    elif terminal.cat == "en":
                        d["x"] = "–" # Substitute en dash
            else:
                # Annotate with terminal name and BÍN meaning (no need to do this for punctuation)
                d["t"] = terminal.name
                if meaning is not None:
                    if terminal.first == "fs":
                        # Special case for prepositions since they're really
                        # resolved from the preposition list in Main.conf, not from BÍN
                        m = (meaning.ordmynd, "fs", "alm", terminal.variant(0).upper())
                    else:
                        m = (meaning.stofn, meaning.ordfl, meaning.fl, meaning.beyging)
                    d["m"] = m
                    # Note the word stem and category
                    wt = WordTuple(stem = m[0].replace("-", ""), cat = m[1])
                elif t.kind == TOK.ENTITY:
                    wt = WordTuple(stem = t.txt, cat = "entity")
        if t.kind != TOK.WORD:
            # Optimize by only storing the k field for non-word tokens
            d["k"] = t.kind
        if t.val is not None and t.kind not in { TOK.WORD, TOK.ENTITY, TOK.PUNCTUATION }:
            # For tokens except words, entities and punctuation, include the val field
            if t.kind == TOK.PERSON:
                case = None
                gender = None
                if terminal is not None and terminal.num_variants >= 1:
                    print("terminal.variants are {0}".format(terminal.variants))
                    gender = terminal.variant(-1)
                    if gender in { "nf", "þf", "þgf", "ef" }:
                        # Oops, mistaken identity
                        case = gender
                        gender = None
                    if terminal.num_variants >= 2:
                        case = terminal.variant(-2)
                print("gender is {0}, case is {1}, t.val is {2}".format(gender, case, t.val))
                fn_list = [ (fn, g, c) for fn, g, c in t.val if (gender is None or g == gender) and (case is None or c == case) ]
                print("fn_list is {0}".format(fn_list))
                # If there are many choices, select the nominative case, or the first element as a last resort
                fn = next((fn for fn in fn_list if fn[2] == "nf"), fn_list[0])
                d["v"] = fn[0] # Include only the name of the person in nominal form
                # Hack to make sure that the gender information is communicated in
                # the terminal name (in some cases the terminal only contains the case)
                if gender is None:
                    gender = fn[1]
                if terminal:
                    if not terminal.name.endswith("_" + gender):
                        d["t"] = terminal.name + "_" + gender
                else:
                    # There is no terminal: cop out by adding a separate gender field
                    d["g"] = gender
                wt = WordTuple(stem = d["v"], cat = "person_" + gender)
            else:
                d["v"] = t.val
        return d, wt

    class _Annotator(ParseForestNavigator):

        """ Local utility subclass to navigate a parse forest and annotate the
            original token list with the corresponding terminal matches """

        def __init__(self, tmap):
            super().__init__()
            self._tmap = tmap

        def _visit_token(self, level, node):
            """ At token node """
            ix = node.token.index # Index into original sentence
            assert ix not in self._tmap
            meaning = node.token.match_with_meaning(node.terminal)
            self._tmap[ix] = (node.terminal, None if isinstance(meaning, bool) else meaning) # Map from original token to matched terminal
            return None

    class _Simplifier(ParseForestNavigator):

        """ Local utility subclass to navigate a parse forest and return a
            simplified, condensed representation of it in a nested dictionary
            structure """

        NT_MAP = {
            "S0" : "P",
            "HreinYfirsetning" : "S",
            "Setning" : "S",
            "SetningSo" : "VP",
            "SetningLo" : "S",
            "SetningÁnF" : "S",
            "SetningAukafall" : "S",
            "SetningSkilyrði" : "S",
            "Skilyrði" : "S-COND",
            "Afleiðing" : "S-CONS",
            "Nl" : "NP",
            "EfLiður" : "NP-POSS",
            "EfLiðurForskeyti" : "NP-POSS",
            "FsMeðFallstjórn" : "PP",
            "SagnInnskot" : "ADVP",
            "FsAtv" : "ADVP",
            "AtvFs" : "ADVP",
            "SagnRuna" : "VP",
            "NhLiðir" : "VP",
            "SagnliðurÁnF" : "VP"
        }

        ID_MAP = {
            "P" : dict(name = "Málsgrein"),
            "S" : dict(name = "Setning", subject_to = "S"),
            "S-COND" : dict(name = "Forsenda", overrides = "S"), # Condition
            "S-CONS" : dict(name = "Afleiðing", overrides = "S"), # Consequence
            "VP" : dict(name = "Sagnliður"),
            "NP" : dict(name = "Nafnliður"),
            "NP-POSS" : dict(name = "Eignarfallsliður", overrides = "NP"),
            "PP" : dict(name = "Forsetningarliður", overrides = "ADVP"),
            "ADVP" : dict(name = "Atviksliður", subject_to = "ADVP")
        }

        def __init__(self, tokens):
            super().__init__(visit_all = True)
            self._tokens = tokens
            self._result = []
            self._stack = [ self._result ]
            self._pushed = []
            self._scope = [ NotImplemented ] # Sentinel value

        def _visit_token(self, level, node):
            """ At token node """
            meaning = node.token.match_with_meaning(node.terminal)
            d, _ = Article._describe_token(self._tokens[node.token.index], node.terminal,
                None if isinstance(meaning, bool) else meaning)
            # Convert from compact form to external (more verbose and descriptive) form
            canonicalize_token(d)
            # Add as a child of the current node in the condensed tree
            self._stack[-1].append(d)
            return None

        def _visit_nonterminal(self, level, node):
            """ Entering a nonterminal node """
            self._pushed.append(False)
            if node.is_interior or node.nonterminal.is_optional:
                return None
            mapped_nt = self.NT_MAP.get(node.nonterminal.first)
            if mapped_nt is not None:
                # We want this nonterminal in the simplified tree:
                # push it (unless it is subject to a scope we're already in)
                mapped_id = self.ID_MAP[mapped_nt]
                subject_to = mapped_id.get("subject_to")
                if self._scope[-1] == subject_to:
                    # We are already within a nonterminal to which this one is subject:
                    # don't bother pushing it
                    return None
                children = []
                self._stack[-1].append(dict(k = "NONTERMINAL",
                    n = mapped_id["name"], i = mapped_nt, p = children))
                self._stack.append(children)
                self._scope.append(mapped_nt)
                self._pushed[-1] = True
            return None

        def _process_results(self, results, node):
            """ Exiting a nonterminal node """
            if not self._pushed.pop():
                return
            # Pushed this nonterminal in _visit_nonterminal(): pop it
            children = self._stack[-1]
            self._stack.pop()
            self._scope.pop()
            # Check whether this nonterminal has only one child, which is again
            # the same nonterminal - or a nonterminal which the parent overrides
            if len(children) == 1:

                def collapse_child(ch0, nt):
                    """ Determine whether to cut off a child and connect directly
                        from this node to its children """
                    d = self.NT_MAP[nt]
                    if ch0["i"] == d:
                        # Same nonterminal category: do the cut
                        return True
                    # If the child is a nonterminal that this one 'overrides',
                    # cut off the child
                    override = self.ID_MAP[d].get("overrides")
                    return ch0["i"] == override

                def replace_parent(ch0, nt):
                    d = self.NT_MAP[nt]
                    # If the child overrides the parent, replace the parent
                    override = self.ID_MAP[ch0["i"]].get("overrides")
                    return d == override

                ch0 = children[0]
                if ch0["k"] == "NONTERMINAL":
                    if collapse_child(ch0, node.nonterminal.first):
                        # If so, we eliminate one level and move the children of the child
                        # up to be children of this node
                        self._stack[-1][-1]["p"] = ch0["p"]
                    elif replace_parent(ch0, node.nonterminal.first):
                        # The child subsumes the parent: replace
                        # the parent by the child
                        self._stack[-1][-1] = ch0

        @property
        def result(self):
            return self._result[0]

    @staticmethod
    def _terminal_map(tree):
        """ Return a dict containing a map from original token indices to matched terminals """
        tmap = dict()
        if tree is not None:
            Article._Annotator(tmap).go(tree)
        return tmap

    @staticmethod
    def _dump_tokens(tokens, tree, words, error_index = None):

        """ Generate a string (JSON) representation of the tokens in the sentence.

            The JSON token dict contents are as follows:

                t.x is original token text.
                t.k is the token kind (TOK.xxx). If omitted, the kind is TOK.WORD.
                t.t is the name of the matching terminal, if any.
                t.m is the BÍN meaning of the token, if any, as a tuple as follows:
                    t.m[0] is the lemma (stofn)
                    t.m[1] is the word category (ordfl)
                    t.m[2] is the word subcategory (fl)
                    t.m[3] is the word meaning/declination (beyging)
                t.v contains auxiliary information, depending on the token kind
                t.err is 1 if the token is an error token

            This function has the side effect of filling in the words dictionary
            with (stem, cat) keys and occurrence counts.

        """

        # Map tokens to associated terminals, if any
        tmap = Article._terminal_map(tree) # tmap is an empty dict if there's no parse tree
        dump = []
        for ix, token in enumerate(tokens):
            # We have already cut away paragraph and sentence markers (P_BEGIN/P_END/S_BEGIN/S_END)
            terminal, meaning = tmap.get(ix, (None, None))
            d, wt = Article._describe_token(token, terminal, meaning)
            if ix == error_index:
                # Mark the error token, if present
                d["err"] = 1
            dump.append(d)
            if words is not None and wt is not None:
                # Add the (stem, cat) combination to the words dictionary
                words[wt] += 1
        return dump

    @staticmethod
    def _simplify_tree(tokens, tree):
        """ Return a simplified parse tree for a sentence, including POS-tagged,
            normalized terminal leaves """
        """ Return a dict containing a map from original token indices to matched terminals """
        if tree is None:
            return None
        s = Article._Simplifier(tokens)
        s.go(tree)
        return s.result

    @staticmethod
    def _process_text(session, text, all_names, xform):
        """ Low-level utility function to parse text and return the result of
            a transformation function (xform) for each sentence """

        t0 = time.time()
        # Demarcate paragraphs in the input
        text = Fetcher.mark_paragraphs(text)
        # Tokenize the result
        toklist = list(tokenize(text, enclosing_session = session))
        t1 = time.time()
        pgs, stats, register = Article._process_toklist(session, toklist, all_names, xform)
        t2 = time.time()
        stats["tok_time"] = t1 - t0
        stats["parse_time"] = t2 - t1
        stats["total_time"] = t2 - t0
        return (pgs, stats, register)

    @staticmethod
    def _process_toklist(session, toklist, all_names, xform):
        """ Low-level utility function to parse token lists and return
            the result of a transformation function (xform) for each sentence """
        pgs = [] # Paragraph list, containing sentences, containing tokens
        with Fast_Parser(verbose = False) as bp: # Don't emit diagnostic messages

            ip = IncrementalParser(bp, toklist, verbose = True)

            for p in ip.paragraphs():
                pgs.append([])
                for sent in p.sentences():
                    if sent.parse():
                        # Parsed successfully
                        pgs[-1].append(xform(sent.tokens, sent.tree, None))
                    else:
                        # Errror in parse
                        pgs[-1].append(xform(sent.tokens, None, sent.err_index))

            stats = dict(
                num_tokens = ip.num_tokens,
                num_sentences = ip.num_sentences,
                num_parsed = ip.num_parsed,
                ambiguity = ip.ambiguity,
            )

        # Add a name register to the result
        register = create_name_register(toklist, session, all_names = all_names)
        return (pgs, stats, register)

    @staticmethod
    def tag_text(session, text, all_names = False):
        """ Parse plain text and return the parsed paragraphs as lists of sentences
            where each sentence is a list of tagged tokens """

        def xform(tokens, tree, err_index):
            """ Transformation function that simply returns a list of POS-tagged,
                normalized tokens for the sentence """
            return Article._dump_tokens(tokens, tree, None, err_index)

        return Article._process_text(session, text, all_names, xform)

    @staticmethod
    def tag_toklist(session, toklist, all_names = False):
        """ Parse plain text and return the parsed paragraphs as lists of sentences
            where each sentence is a list of tagged tokens """

        def xform(tokens, tree, err_index):
            """ Transformation function that simply returns a list of POS-tagged,
                normalized tokens for the sentence """
            return Article._dump_tokens(tokens, tree, None, err_index)

        return Article._process_toklist(session, toklist, all_names, xform)

    @staticmethod
    def parse_text(session, text, all_names = False):
        """ Parse plain text and return the parsed paragraphs as simplified trees """

        def xform(tokens, tree, err_index):
            """ Transformation function that yields a simplified parse tree
                with POS-tagged, normalized terminal leaves for the sentence """
            if err_index is not None:
                return Article._dump_tokens(tokens, tree, None, err_index)
            # Successfully parsed: return a simplified tree for the sentence
            return Article._simplify_tree(tokens, tree)

        return Article._process_text(session, text, all_names, xform)

    def person_names(self):
        """ A generator yielding all person names in an article token stream """
        if self._raw_tokens is None and self._tokens:
            # Lazy generation of the raw tokens from the JSON rep
            self._raw_tokens = json.loads(self._tokens)
        if self._raw_tokens:
            for p in self._raw_tokens:
                for sent in p:
                    for t in sent:
                        if t.get("k") == TOK.PERSON:
                            # The full name of the person is in the v field
                            yield t["v"]

    def entity_names(self):
        """ A generator for entity names from an article token stream """
        if self._raw_tokens is None and self._tokens:
            # Lazy generation of the raw tokens from the JSON rep
            self._raw_tokens = json.loads(self._tokens)
        if self._raw_tokens:
            for p in self._raw_tokens:
                for sent in p:
                    for t in sent:
                        if t.get("k") == TOK.ENTITY:
                            # The entity name
                            yield t["x"]

    def create_register(self, session, all_names = False):
        """ Create a name register dictionary for this article """
        register = { }
        for name in self.person_names():
            add_name_to_register(name, register, session, all_names = all_names)
        # Add register of entity names
        for name in self.entity_names():
            add_entity_to_register(name, register, session, all_names = all_names)
        return register

    def _store_words(self, session):
        """ Store word stems """
        assert session is not None
        # Delete previously stored words for this article
        session.execute(Word.table().delete().where(Word.article_id == self._uuid))
        # Index the words by storing them in the words table
        for word, cnt in self._words.items():
            if word.cat not in NoIndexWords.CATEGORIES_TO_INDEX:
                # We do not index closed word categories and non-distinctive constructs
                continue
            if (word.stem, word.cat) in NoIndexWords.SET:
                # Specifically excluded from indexing in Reynir.conf (Main.conf)
                continue
            # Interesting word: let's index it
            w = Word(
                article_id = self._uuid,
                stem = word.stem,
                cat = word.cat,
                cnt = cnt
            )
            session.add(w)

    def _parse(self, enclosing_session = None, verbose = False):
        """ Parse the article content to yield parse trees and annotated token list """
        with SessionContext(enclosing_session) as session:

            # Convert the content soup to a token iterable (generator)
            toklist = Fetcher.tokenize_html(self._url, self._html, session)

            bp = self.get_parser()
            ip = IncrementalParser(bp, toklist, verbose = verbose)

            # List of paragraphs containing a list of sentences containing token lists
            # for sentences in string dump format (1-based paragraph and sentence indices)
            pgs = []

            # Dict of parse trees in string dump format,
            # stored by sentence index (1-based)
            trees = OrderedDict()

            # Word stem dictionary, indexed by (stem, cat)
            words = defaultdict(int)
            num_sent = 0

            for p in ip.paragraphs():

                pgs.append([])

                for sent in p.sentences():

                    num_sent += 1

                    #if Settings.DEBUG:
                    #    print("Parsing: {0}".format(sent))

                    if sent.parse():
                        # Obtain a text representation of the parse tree
                        trees[num_sent] = ParseForestDumper.dump_forest(sent.tree)
                        pgs[-1].append(Article._dump_tokens(sent.tokens, sent.tree, words))
                    else:
                        # Error or no parse: add an error index entry for this sentence
                        eix = sent.err_index
                        trees[num_sent] = "E{0}".format(eix)
                        pgs[-1].append(Article._dump_tokens(sent.tokens, None, None, eix))

            parse_time = ip.parse_time

            self._parsed = datetime.utcnow()
            self._parser_version = bp.version
            self._num_tokens = ip.num_tokens
            self._num_sentences = ip.num_sentences
            self._num_parsed = ip.num_parsed
            self._ambiguity = ip.ambiguity

            # Make one big JSON string for the paragraphs, sentences and tokens
            self._raw_tokens = pgs
            self._tokens = json.dumps(pgs, separators = (',', ':'), ensure_ascii = False)

            # Keep the bag of words (stem, category, count for each word)
            self._words = words

            # Create a tree representation string out of all the accumulated parse trees
            self._tree = "".join("S{0}\n{1}\n".format(key, val) for key, val in trees.items())


    def store(self, enclosing_session = None):
        """ Store an article in the database, inserting it or updating """
        with SessionContext(enclosing_session, commit = True) as session:
            if self._uuid is None:
                # Insert a new row
                self._uuid = str(uuid.uuid1())
                ar = ArticleRow(
                    id = self._uuid,
                    url = self._url,
                    root_id = self._root_id,
                    heading = self._heading,
                    author = self._author,
                    timestamp = self._timestamp,
                    authority = self._authority,
                    scraped = self._scraped,
                    parsed = self._parsed,
                    processed = self._processed,
                    indexed = self._indexed,
                    scr_module = self._scr_module,
                    scr_class = self._scr_class,
                    scr_version = self._scr_version,
                    parser_version = self._parser_version,
                    num_sentences = self._num_sentences,
                    num_parsed = self._num_parsed,
                    ambiguity = self._ambiguity,
                    html = self._html,
                    tree = self._tree,
                    tokens = self._tokens
                )
                session.add(ar)
                if self._words:
                    # Store the word stems occurring in the article
                    self._store_words(session)
                return True

            # Update an already existing row by UUID
            ar = session.query(ArticleRow).filter(ArticleRow.id == self._uuid).one_or_none()
            if ar is None:
                # UUID not found: something is wrong here...
                return False

            # Update the columns
            # UUID is immutable
            ar.url = self._url
            ar.root_id = self._root_id
            ar.heading = self._heading
            ar.author = self._author
            ar.timestamp = self._timestamp
            ar.authority = self._authority
            ar.scraped = self._scraped
            ar.parsed = self._parsed
            ar.processed = self._processed
            ar.indexed = self._indexed
            ar.scr_module = self._scr_module
            ar.scr_class = self._scr_class
            ar.scr_version = self._scr_version
            ar.parser_version = self._parser_version
            ar.num_sentences = self._num_sentences
            ar.num_parsed = self._num_parsed
            ar.ambiguity = self._ambiguity
            ar.html = self._html
            ar.tree = self._tree
            ar.tokens = self._tokens
            if self._words is not None:
                # If the article has been parsed, update the index of word stems
                # (This may cause all stems for the article to be deleted, if
                # there are no successfully parsed sentences in the article)
                self._store_words(session)
            return True

    def prepare(self, enclosing_session = None, verbose = False, reload_parser = False):
        """ Prepare the article for display. If it's not already tokenized and parsed, do it now. """
        with SessionContext(enclosing_session, commit = True) as session:
            if self._tree is None or self._tokens is None:
                if reload_parser:
                    # We need a parse: Make sure we're using the newest grammar
                    self.reload_parser()
                self._parse(session, verbose = verbose)
                if self._tree is not None or self._tokens is not None:
                    # Store the updated article in the database
                    self.store(session)

    def parse(self, enclosing_session = None, verbose = False, reload_parser = False):
        """ Force a parse of the article """
        with SessionContext(enclosing_session, commit = True) as session:
            if reload_parser:
                # We need a parse: Make sure we're using the newest grammar
                self.reload_parser()
            self._parse(session, verbose = verbose)
            if self._tree is not None or self._tokens is not None:
                # Store the updated article in the database
                self.store(session)


    @property
    def url(self):
        return self._url

    @property
    def uuid(self):
        return self._uuid

    @property
    def heading(self):
        return self._heading

    @property
    def author(self):
        return self._author

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def num_sentences(self):
        return self._num_sentences

    @property
    def num_parsed(self):
        return self._num_parsed

    @property
    def ambiguity(self):
        return self._ambiguity

    @property
    def root_domain(self):
        return self._root_domain

    @property
    def html(self):
        return self._html

    @property
    def tree(self):
        return self._tree

    @property
    def tokens(self):
        return self._tokens

    @property
    def num_tokens(self):
        """ Count the tokens in the article and cache the result """
        if self._num_tokens is None:
            if self._raw_tokens is None and self._tokens:
                self._raw_tokens = json.loads(self._tokens)
            cnt = 0
            if self._raw_tokens:
                for p in self._raw_tokens:
                    for sent in p:
                        cnt += len(sent)
            self._num_tokens = cnt
        return self._num_tokens

