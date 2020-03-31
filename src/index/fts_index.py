# anki-search-inside-add-card
# Copyright (C) 2019 - 2020 Tom Z.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sqlite3
import os
import sys
import struct
import re
import time
import math
import collections
from aqt import *
from aqt.utils import showInfo, tooltip

from ..output import *
from ..debug_logging import log, persist_index_info
from ..models import IndexNote, SiacNote
import utility.misc
import utility.text

class FTSIndex:

    def __init__(self, corpus, index_up_to_date):

        self.limit = 20
        self.pinned = []
        self.highlighting = True
        self.dir = utility.misc.get_user_files_folder_path()
        self.stopWords = []
        # mid : [fld_ord]
        self.fields_to_exclude = {}
        # stores values useful to determine whether the index has to be rebuilt on restart or not
        self.creation_info = {}
        self.threadPool = QThreadPool()
        self.ui = Output()

        config = mw.addonManager.getConfig(__name__)
        try:
            self.stopWords = set(config['stopwords'])
        except KeyError:
            self.stopWords = []
        self.creation_info["stopwords_size"] = len(self.stopWords)
        self.creation_info["decks"] = config["decks"]
        self.porter = config["usePorterStemmer"]
        #exclude fields
        try:
            self.fields_to_exclude = config['fieldsToExclude']
            self.creation_info["fields_to_exclude_original"] = self.fields_to_exclude
        except KeyError:
            self.fields_to_exclude = {}
        self.ui.fields_to_exclude = self.fields_to_exclude

        self.creation_info["index_was_rebuilt"] = not index_up_to_date
        if not index_up_to_date:
            if self.porter:
                sql = "create virtual table notes using fts%s(nid, text, tags, did, source, mid, refs, tokenize=porter)"
            else:
                sql = "create virtual table notes using fts%s(nid, text, tags, did, source, mid, refs)"

            cleaned = self._cleanText(corpus)
            try:
                os.remove(self.dir + "search-data.db")
            except OSError:
                pass
            conn = sqlite3.connect(self.dir + "search-data.db")
            conn.execute("drop table if exists notes")
            try:
                conn.execute(sql % 5)
                self.type = "SQLite FTS5"
            except:
                try:
                    conn.execute(sql % 4)
                    self.type = "SQLite FTS4"
                except:
                    conn.execute(sql % 3)
                    self.type = "SQlite FTS3"

            conn.executemany('INSERT INTO notes VALUES (?,?,?,?,?,?,?)', cleaned)
            conn.execute("INSERT INTO notes(notes) VALUES('optimize')")
            conn.commit()
            conn.close()
        else:
            self.type = self._check_fts_version(config["logging"])
        if not index_up_to_date:
            persist_index_info(self)


    def _check_fts_version(self, logging):
        con = sqlite3.connect(':memory:')
        cur = con.cursor()
        cur.execute('pragma compile_options;')
        available_pragmas = [s[0].lower() for s in cur.fetchall()]
        con.close()
        if logging:
            log("\nSQlite compile options: " + str(available_pragmas))
        if 'enable_fts5' in available_pragmas:
            return "SQLite FTS5"
        return "SQLite FTS4"
       


    def _cleanText(self, corpus):
        filtered = list()
        text = ""
        for row in corpus:
            text = row[1]
            #if the notes model id is in our filter dict, that means we want to exclude some field(s)
            if row[4] in self.fields_to_exclude:
                text = utility.text.remove_fields(text, self.fields_to_exclude[row[4]])
            text = utility.text.clean(text, self.stopWords)
            filtered.append((row[0], text, row[2], row[3], row[1], row[4], row[5]))
        return filtered

    def removeStopwords(self, text):
        cleaned = ""
        for token in text.split(" "):
            if token.lower() not in self.stopWords:
                cleaned += token + " "
        if len(cleaned) > 0:
            return cleaned[:-1]
        return ""


    def search(self, text, decks, only_user_notes = False, print_mode = "default"):
        """
        Search for the given text.
        Args:
        text - string to search, typically fields content
        decks - list of deck ids, if -1 is contained, all decks are searched
        """
        worker = Worker(self.searchProc, text, decks, only_user_notes, print_mode)
        worker.stamp = utility.misc.get_milisec_stamp()
        self.ui.latest = worker.stamp
        if print_mode == "default":
            worker.signals.result.connect(self.printOutput)
        elif print_mode == "pdf":
            worker.signals.result.connect(self.print_pdf)

        worker.signals.tooltip.connect(self.ui.show_tooltip)
        self.threadPool.start(worker)


    def searchProc(self, text, decks, only_user_notes, print_mode):
        resDict = {}
        start = time.time()
        orig = text
        text = self.clean(text)
        resDict["time-stopwords"] = int((time.time() - start) * 1000)
        if self.logging:
            log("\nFTS index - Received query: " + text)
            log("Decks (arg): " + str(decks))
            log("Self.pinned: " + str(self.pinned))
            log("Self.limit: " +str(self.limit))
        self.lastSearch = (text, decks, "default")

        if len(text) == 0:
            if print_mode == "default":
                self.ui.empty_result("Query was empty after cleaning.<br/><br/><b>Query:</b> <i>%s</i>" % utility.text.trim_if_longer_than(orig, 100).replace("\u001f", ""))
                if mw.addonManager.getConfig(__name__)["hideSidebar"]:
                    return "Found 0 notes. Query was empty after cleaning."
                return None
            elif print_mode == "pdf":
                return None

        start = time.time()
        text = utility.text.expand_by_synonyms(text, self.synonyms)
        resDict["time-synonyms"] = int((time.time() - start) * 1000)
        resDict["query"] = text
        if utility.text.text_too_small(text):
            if self.logging:
                log("Returning - Text was < 2 chars: " + text)
            return { "results" : [] }

        tokens = text.split(" ")
        if len(tokens) > 10:
            tokens = set(tokens)
        if self.type == "SQLite FTS5":
            query = u" OR ".join(["tags:" + s.strip().replace("OR", "or") for s in tokens if not utility.text.text_too_small(s) ])
            query += " OR " + " OR ".join(["text:" + s.strip().replace("OR", "or") for s in tokens if not utility.text.text_too_small(s) ])
        else:
            query = " OR ".join([s.strip().replace("OR", "or") for s in tokens if not utility.text.text_too_small(s) ])
        if len(query) == 0 or query == " OR ":
            if self.logging:
                log("Returning. Query was: " + query)
            return { "results" : [] }

        c = 0
        resDict["decks"] = decks
        allDecks = "-1" in decks
        decks.append("-1")
        rList = list()
        user_note_filter = "AND mid='-1'" if only_user_notes else ""
        conn = sqlite3.connect(self.dir + "search-data.db")
        if self.type == "SQLite FTS5":
            dbStr = "select nid, text, tags, did, source, bm25(notes) as score, mid, refs from notes where notes match '%s' %s order by score" %(query, user_note_filter)

        else:
            conn.create_function("simple_rank", 1, simple_rank)
            dbStr = "select nid, text, tags, did, source, simple_rank(matchinfo(notes)) as score, mid, refs from notes where text match '%s' %s order by score desc" %(query, user_note_filter)

        try:
            start = time.time()
            res = conn.execute(dbStr).fetchall()
            resDict["time-query"] = int((time.time() - start) * 1000)
        except Exception as e:
            if self.logging:
                log("Executing db query threw exception: " + str(e))
            res = []
        if self.logging:
            log("dbStr was: " + dbStr)
            log("Result length of db query: " + str(len(res)))


        resDict["highlighting"] = self.highlighting
        # if self.type == "SQLite FTS5":
        for r in res:
            if not str(r[0]) in self.pinned and (allDecks or str(r[3]) in decks):
                
                if str(r[6]) == "-1":
                    rList.append(SiacNote.from_index(r))
                else:
                    rList.append(IndexNote(r))
                c += 1
                if c >= self.limit:
                    break

        conn.close()

        if self.logging:
            log("Query was: " + query)
            log("Result length (after removing pinned and unselected decks): " + str(len(rList)))
        resDict["results"] = rList[:min(self.limit, len(rList))]
        self.lastResDict = resDict
        return resDict

    def printOutput(self, result, stamp):
        query_set = None
        if self.highlighting and self.lastResDict is not None and "query" in self.lastResDict and self.lastResDict["query"] is not None:
            query_set =  set(utility.text.replace_accents_with_vowels(s).lower() for s in self.lastResDict["query"].split(" "))
        if type(result) is str:
            #self.output.show_tooltip(result)
            pass
        elif result is not None:
            self.ui.print_search_results(result["results"], stamp, logging = self.logging, printTimingInfo = True, query_set=query_set)


    def print_pdf(self, result, stamp):
        query_set = None
        if self.lastResDict is not None and "query" in self.lastResDict and self.lastResDict["query"] is not None:
            query_set =  set(utility.text.replace_accents_with_vowels(s).lower() for s in self.lastResDict["query"].split(" "))
        if result is not None:
            self.ui.print_pdf_search_results(result["results"], stamp, query_set)
        else:
            self.ui.print_pdf_search_results([], stamp, self.lastSearch[0])


    def searchDB(self, text, decks):
        """
        Used for searches in the search mask,
        doesn't use the index, instead use the traditional anki search 
        """
        stamp = utility.misc.get_milisec_stamp()
        self.ui.latest = stamp
        found = self.finder.findNotes(text)

        if len (found) > 0:
            if not "-1" in decks:
                deckQ =  "(-1, %s)" % ",".join(decks)
            else:
                deckQ = ""
            #query db with found ids
            foundQ = "(%s)" % ",".join([str(f) for f in found])
            if deckQ:
                res = mw.col.db.all("select distinct notes.id, flds, tags, did, notes.mid from notes left join cards on notes.id = cards.nid where nid in %s and did in %s" %(foundQ, deckQ))
            else:
                res = mw.col.db.all("select distinct notes.id, flds, tags, did, notes.mid from notes left join cards on notes.id = cards.nid where nid in %s" %(foundQ))
            rList = []
            for r in res:
                #pinned items should not appear in the results
                if not str(r[0]) in self.pinned:
                    #todo: implement highlighting
                    rList.append(IndexNote((r[0], r[1], r[2], r[3], r[1], -1, r[4], "")))
            return { "result" : rList[:self.limit], "stamp" : stamp }
        return { "result" : [], "stamp" : stamp }

 

    def clean(self, text):
        return utility.text.clean(text, self.stopWords)

   
    def deleteNote(self, nid):
        conn = sqlite3.connect(self.dir + "search-data.db")
        conn.cursor().execute("DELETE FROM notes WHERE CAST(nid AS INTEGER) = %s;" % nid)
        conn.commit()
        conn.close()

    def add_user_note(self, note):
        """
        Add a non-anki note to the index.
        """
        text = utility.text.build_user_note_text(title=note[1], text=note[2], source=note[3])
        conn = sqlite3.connect(self.dir + "search-data.db")
        conn.cursor().execute("INSERT INTO notes (nid, text, tags, did, source, mid, refs) VALUES (?, ?, ?, ?, ?, ?, '')", (note[0], utility.text.clean(text, self.stopWords), note[4], "-1", text, "-1"))
        conn.commit()
        conn.close()
        persist_index_info(self)

    def update_user_note(self, note):
        """
            Deletes and adds the given user note again with updated values.
        """
        self.deleteNote(int(note[0]))
        self.add_user_note(note)


    def addNote(self, note):
        content = " \u001f ".join(note.fields)
        tags = " ".join(note.tags)
        #did = note.model()['did']
        did = mw.col.db.first("select distinct did from notes left join cards on notes.id = cards.nid where nid = %s" % note.id)
        if did is None or len(did) == 0:
            return
        did = did[0]
        if str(note.mid) in self.fields_to_exclude:
            content = utility.text.remove_fields(content, self.fields_to_exclude[str(note.mid)])
        conn = sqlite3.connect(self.dir + "search-data.db")
        conn.cursor().execute("INSERT INTO notes (nid, text, tags, did, source, mid, refs) VALUES (?, ?, ?, ?, ?, ?, '')", (note.id, utility.text.clean(content, self.stopWords), tags, did, content, note.mid))
        conn.commit()
        conn.close()
        persist_index_info(self)

    def updateNote(self, note):
        self.deleteNote(note.id)
        self.addNote(note)

    def get_last_inserted_id(self):
        conn = sqlite3.connect(self.dir + "search-data.db")
        row_id = conn.cursor().execute("SELECT id FROM notes_content ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        return row_id

    def get_number_of_notes(self):
        try:
            conn = sqlite3.connect(self.dir + "search-data.db")
            res = conn.cursor().execute("select count(*) from notes_content").fetchone()[0]
            conn.close()
            return res
        except:
            return 0


def _parseMatchInfo(buf):
    bufsize = len(buf)
    return [struct.unpack('@I', buf[i:i+4])[0] for i in range(0, bufsize, 4)]

def simple_rank(rawMatchInfo):
    """
    Based on https://github.com/saaj/sqlite-fts-python/blob/master/sqlitefts/ranking.py
    """
    match_info = _parseMatchInfo(rawMatchInfo)
    score = 0.0
    p, c = match_info[:2]
    for phrase_num in range(p):
        phrase_info_idx = 2 + (phrase_num * c * 3)
        for col_num in range(c):
            col_idx = phrase_info_idx + (col_num * 3)
            x1, x2 = match_info[col_idx:col_idx + 2]
            if x1 > 0:
                score += float(x1) / x2
    return score


def bm25(rawMatchInfo):
    match_info = _parseMatchInfo(rawMatchInfo)
    K = 0.5
    B = 0.75
    score = 0.0

    P_O, C_O, N_O, A_O = range(4)
    term_count = match_info[P_O]
    col_count = match_info[C_O]
    total_docs = match_info[N_O]
    L_O = A_O + col_count
    X_O = L_O + col_count

    weights = [1] * col_count
    #collect number of different matched terms
    # cd = 0
    # for i in range(term_count):
    #     for j in range(col_count):
    #         x = X_O + (3 * j * (i + 1))
    #         if float(match_info[x]) != 0.0:
    #             cd += 1

    for i in range(term_count):
        for j in range(col_count):
            weight = weights[j]
            if weight == 0:
                continue

            avg_length = float(match_info[A_O + j])
            doc_length = float(match_info[L_O + j])
            if avg_length == 0:
                D = 0
            else:
                D = 1 - B + (B * (doc_length / avg_length))

            x = X_O + (3 * j * (i + 1))
            term_frequency = float(match_info[x])
            docs_with_term = float(match_info[x + 2])

            idf = max(
                math.log(
                    (total_docs - docs_with_term + 0.5) /
                    (docs_with_term + 0.5)),
                0)
            denom = term_frequency + (K * D)
            if denom == 0:
                rhs = 0
            else:
                rhs = (term_frequency * (K + 1)) / denom

            score += (idf * rhs) * weight
    return score


class Worker(QRunnable):

    def __init__(self, fn, *args):
        super(Worker, self).__init__()
        self.fn = fn
        self.args = args
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        '''
        Initialise the runner function with passed args, kwargs.
        '''

        try:
            result = self.fn(*self.args)
        except:
            traceback.print_exc()
            exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((exctype, value, traceback.format_exc()))
        else:
            #use stamp to track time
            self.signals.result.emit(result, self.stamp)
        finally:
            self.signals.finished.emit()

class WorkerSignals(QObject):

    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object, object)
    progress = pyqtSignal(int)
    tooltip = pyqtSignal(str)
