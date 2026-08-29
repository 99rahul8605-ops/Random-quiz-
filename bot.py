import os
import json
import random
import asyncio
import csv
import threading
import re
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PollAnswerHandler
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from bson.objectid import ObjectId

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
PORT = int(os.getenv('PORT', 10000))
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/quizbot')
# NEW: shown as the "🆘 Support" button on /start — set this to your support @username (no @) or a t.me link
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '').lstrip('@').strip()

# Global bot instance
bot_instance = None

class MongoDB:
    def __init__(self, uri):
        self.uri = uri
        self.client = None
        self.db = None
        self.connect()
    
    def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = MongoClient(self.uri)
            self.db = self.client.quizbot
            # Test connection
            self.client.admin.command('ping')
            print("✅ Connected to MongoDB successfully!")
            # NEW: ensure indexes for hierarchical quiz queries
            try:
                quizzes_col = self.db['quizzes']
                quizzes_col.create_index([('subject', 1), ('folder', 1), ('is_active', 1)])
                quizzes_col.create_index([('subject', 1), ('folder', 1), ('subfolder', 1), ('is_active', 1)])
                quizzes_col.create_index([('is_active', 1)])
                print("✅ Database indexes ensured (subject, folder, is_active)")
            except Exception as idx_error:
                print(f"⚠️ Index creation skipped: {idx_error}")
        except ConnectionFailure as e:
            print(f"❌ MongoDB connection failed: {e}")
            # Fallback to in-memory storage
            self.db = None
    
    def is_connected(self):
        """Check if MongoDB is connected"""
        return self.db is not None
    
    def get_collection(self, name):
        """Get a collection from MongoDB"""
        if self.db is not None:
            return self.db[name]
        return None
    
    def insert_one(self, collection_name, document):
        """Insert one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.insert_one(document)
        return None
    
    def find(self, collection_name, query=None):
        """Find documents"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return list(collection.find(query or {}))
        return []
    
    def find_one(self, collection_name, query):
        """Find one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.find_one(query)
        return None
    
    def update_one(self, collection_name, query, update):
        """Update one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.update_one(query, update)
        return None
    
    # NEW: bulk update (used for renames and migration)
    def update_many(self, collection_name, query, update):
        """Update multiple documents"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.update_many(query, update)
        return None
    
    def delete_one(self, collection_name, query):
        """Delete one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.delete_one(query)
        return None
    
    def delete_many(self, collection_name, query):
        """Delete multiple documents"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.delete_many(query)
        return None
    
    def replace_one(self, collection_name, query, replacement):
        """Replace one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.replace_one(query, replacement)
        return None
    
    # NEW: distinct values (subjects / folders) without loading full documents
    def distinct(self, collection_name, key, query=None):
        """Get distinct values for a key"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.distinct(key, query or {})
        return []
    
    # NEW: aggregation pipeline support (folder counts etc.)
    def aggregate(self, collection_name, pipeline):
        """Run an aggregation pipeline"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return list(collection.aggregate(pipeline))
        return []
    
    # NEW: efficient counting
    def count_documents(self, collection_name, query=None):
        """Count documents matching a query"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.count_documents(query or {})
        return 0

class QuizBot:
    def __init__(self):
        self.application = None
        self.mongo = MongoDB(MONGODB_URI)
        self.migrate_quizzes()  # NEW: safe migration BEFORE first load
        self.quizzes = self.load_quizzes()
        self.groups = self.load_groups()
        self.settings = self.load_settings()
        self.stats = self.load_stats()
        self.sudo_users = self.load_sudo_users()  # NEW: load sudo users
        self.broadcast_mode = {}
        self.scheduler_task = None
        self.quiz_interval = self.settings.get('quiz_interval', 3600)  # Default 1 hour
        self.recently_sent_quizzes = []  # Track recently sent quiz IDs
        self.max_recent_track = 10  # Keep track of last 10 sent quizzes
        # NEW: hierarchical quiz storage support
        # token -> subject name  and  token -> (subject, folder)
        # Keeps callback_data short (Telegram 64-byte limit) even for long names
        self.subject_tokens = {}
        self.pair_tokens = {}
        # NEW: token -> (subject, folder, subfolder) — subfolder='' means "no sub-folder / all"
        self.qz_ctx_tokens = {}
        # NEW: token -> (subject, (folder1, folder2, ...)) for multi-chapter quiz selection
        self.multi_ctx_tokens = {}
        # NEW: token -> (subject1, subject2, ...) for multi-SUBJECT quiz selection
        self.subj_multi_ctx_tokens = {}
        # NEW: token -> plain name (folder/sub-folder), used by the multi-subject → multi-chapter
        # → multi-sub-folder flow, where names are a UNION across several subjects
        self.name_tokens = {}
        # NEW: token -> (subjects tuple, folders tuple-or-None, subfolders tuple-or-None) for the
        # full multi-subject → multi-chapter → multi-sub-folder quiz selection
        self.subjmulti_full_ctx_tokens = {}
        # NEW: "chat_id:message_id" -> quiz _id (string), so a poll can be traced back to its
        # exact DB document (used by /qreport + admin edit/replace). In-memory only —
        # resets on restart, same trade-off as the other token caches above.
        self.poll_quiz_map = {}
        # NEW: token -> (quiz_id, report_id) for the admin "Edit / Replace Quiz" flow
        self.edit_ctx_tokens = {}
    
    # ==========================================================
    # NEW: HIERARCHICAL QUIZ HELPERS (Subject → Folder → Questions)
    # ==========================================================
    
    def migrate_quizzes(self):
        """Safely migrate old flat quizzes to the hierarchical structure.
        Idempotent: quizzes that already have subject/folder are untouched.
        Old quizzes receive subject='General', folder='Uncategorized'."""
        try:
            collection = self.mongo.get_collection('quizzes')
            if collection is None:
                return
            result1 = collection.update_many(
                {'subject': {'$exists': False}},
                {'$set': {'subject': 'General', 'folder': 'Uncategorized'}}
            )
            result2 = collection.update_many(
                {'folder': {'$exists': False}},
                {'$set': {'folder': 'Uncategorized'}}
            )
            # NEW: sub-folder support — old quizzes get an empty subfolder (= "no sub-folder")
            result3 = collection.update_many(
                {'subfolder': {'$exists': False}},
                {'$set': {'subfolder': ''}}
            )
            migrated = 0
            if result1 is not None:
                migrated += result1.modified_count
            if result2 is not None:
                migrated += result2.modified_count
            if result3 is not None:
                migrated += result3.modified_count
            if migrated:
                print(f"🔄 Migrated {migrated} old quiz(es) → General / Uncategorized")
            else:
                print("✅ Quiz hierarchy migration: nothing to do")
        except Exception as e:
            print(f"⚠️ Quiz migration failed (non-fatal): {e}")
    
    def make_token(self, value):
        """Short deterministic token for callback data (Telegram 64-byte limit)"""
        return hashlib.md5(value.lower().encode('utf-8')).hexdigest()[:10]
    
    def register_subject_token(self, subject):
        """Register and return a short token for a subject name"""
        token = self.make_token(f"subject::{subject}")
        self.subject_tokens[token] = subject
        return token
    
    def resolve_subject_token(self, token):
        """Resolve a subject token back to its name; rebuilds map from DB if needed"""
        if token in self.subject_tokens:
            return self.subject_tokens[token]
        try:
            for subject in self.get_subjects():
                self.register_subject_token(subject)
        except Exception as e:
            print(f"⚠️ Subject token map rebuild failed: {e}")
        return self.subject_tokens.get(token)
    
    def register_pair_token(self, subject, folder):
        """Register and return a short token for a (subject, folder) pair"""
        token = self.make_token(f"pair::{subject}::{folder}")
        self.pair_tokens[token] = (subject, folder)
        return token
    
    def resolve_pair_token(self, token):
        """Resolve a pair token back to (subject, folder); rebuilds map from DB if needed"""
        if token in self.pair_tokens:
            return self.pair_tokens[token]
        try:
            structure = self.get_structure()
            for subject, folders in structure['folders'].items():
                for folder in folders:
                    self.register_pair_token(subject, folder)
        except Exception as e:
            print(f"⚠️ Pair token map rebuild failed: {e}")
        return self.pair_tokens.get(token)
    
    def register_qz_ctx(self, subject, folder, subfolder=''):
        """NEW: Register and return a short token for a (subject, folder, subfolder) context.
        subfolder='' means 'no sub-folder / all questions in the folder'."""
        subfolder = subfolder or ''
        token = self.make_token(f"ctx::{subject}::{folder}::{subfolder}")
        self.qz_ctx_tokens[token] = (subject, folder, subfolder)
        return token

    def resolve_qz_ctx(self, token):
        """NEW: Resolve a qz_ctx token back to (subject, folder, subfolder); rebuilds map from DB if needed"""
        if token in self.qz_ctx_tokens:
            return self.qz_ctx_tokens[token]
        try:
            for subject in self.get_subjects():
                for folder in self.get_folders(subject):
                    self.register_qz_ctx(subject, folder, '')
                    for sf in self.get_subfolders(subject, folder):
                        self.register_qz_ctx(subject, folder, sf)
        except Exception as e:
            print(f"⚠️ Quiz-context token map rebuild failed: {e}")
        return self.qz_ctx_tokens.get(token)

    def register_multi_ctx(self, subject, folders):
        """NEW: Register and return a short token for a (subject, [folders...]) multi-chapter selection."""
        folders = tuple(sorted(folders))
        token = self.make_token(f"multi::{subject}::{'|'.join(folders)}")
        self.multi_ctx_tokens[token] = (subject, folders)
        return token

    def resolve_multi_ctx(self, token):
        """NEW: Resolve a multi-ctx token back to (subject, folders tuple). Not DB-rebuildable
        (arbitrary subset), so if the bot restarted mid-flow the user just needs to reselect."""
        return self.multi_ctx_tokens.get(token)

    def register_subj_multi_ctx(self, subjects):
        """NEW: Register and return a short token for a (subject1, subject2, ...) multi-subject selection."""
        subjects = tuple(sorted(subjects))
        token = self.make_token(f"subjmulti::{'|'.join(subjects)}")
        self.subj_multi_ctx_tokens[token] = subjects
        return token

    def resolve_subj_multi_ctx(self, token):
        """NEW: Resolve a subj-multi-ctx token back to a tuple of subject names."""
        return self.subj_multi_ctx_tokens.get(token)

    def register_name_token(self, name):
        """NEW: Register and return a short token for a plain name (folder/sub-folder),
        used where names are a UNION across several subjects and aren't tied to one pair."""
        token = self.make_token(f"name::{name}")
        self.name_tokens[token] = name
        return token

    def resolve_name_token(self, token):
        """NEW: Resolve a name token back to its plain string. Not DB-rebuildable
        (arbitrary union), so if the bot restarted mid-flow the user just needs to reselect."""
        return self.name_tokens.get(token)

    def register_subjmulti_full_ctx(self, subjects, folders=None, subfolders=None):
        """NEW: Register and return a short token for a full
        (subjects..., folders...-or-None, subfolders...-or-None) selection."""
        subjects = tuple(sorted(subjects))
        folders = tuple(sorted(folders)) if folders else None
        subfolders = tuple(sorted(subfolders)) if subfolders else None
        token = self.make_token(
            f"subjmultifull::{'|'.join(subjects)}::{'|'.join(folders or [])}::{'|'.join(subfolders or [])}")
        self.subjmulti_full_ctx_tokens[token] = (subjects, folders, subfolders)
        return token

    def resolve_subjmulti_full_ctx(self, token):
        """NEW: Resolve a full multi-subject/chapter/sub-folder ctx token back to
        (subjects tuple, folders tuple-or-None, subfolders tuple-or-None)."""
        return self.subjmulti_full_ctx_tokens.get(token)

    def register_edit_ctx(self, quiz_id, report_id):
        """NEW: Register and return a short token for a (quiz_id, report_id) pair,
        used by the admin "Edit / Replace Quiz" flow."""
        token = self.make_token(f"editctx::{quiz_id}::{report_id}")
        self.edit_ctx_tokens[token] = (quiz_id, report_id)
        return token

    def resolve_edit_ctx(self, token):
        """NEW: Resolve an edit-ctx token back to (quiz_id, report_id)."""
        return self.edit_ctx_tokens.get(token)

    def get_union_folders(self, subjects):
        """NEW: {folder_name: total_count} across ALL of the given subjects."""
        structure = self.get_structure()
        counts = {}
        for subj in subjects:
            for name, cnt in structure['folders'].get(subj, {}).items():
                counts[name] = counts.get(name, 0) + cnt
        return counts

    def get_union_subfolders(self, subjects, folders):
        """NEW: Sorted list of distinct sub-folder names across every (subject, folder)
        combination in the given subjects × folders."""
        names = set()
        for subj in subjects:
            for folder in folders:
                names.update(self.get_subfolders(subj, folder))
        return sorted(names)

    def get_subjects(self):
        """Get all distinct subject names (sorted)"""
        try:
            subjects = [s for s in self.mongo.distinct('quizzes', 'subject') if s]
        except Exception:
            subjects = []
        return sorted(subjects)
    
    def get_folders(self, subject):
        """Get all distinct folder names for a subject (sorted)"""
        try:
            folders = self.mongo.distinct('quizzes', 'folder', {'subject': subject})
        except Exception:
            folders = []
        return sorted([f for f in folders if f])
    
    def get_subfolders(self, subject, folder):
        """NEW: Get all distinct sub-folder names inside a subject/folder (sorted).
        Quizzes saved directly in the folder (no sub-folder) are excluded here."""
        try:
            subfolders = self.mongo.distinct('quizzes', 'subfolder', {'subject': subject, 'folder': folder})
        except Exception:
            subfolders = []
        return sorted([s for s in subfolders if s])
    
    def get_structure(self):
        """Return {'subjects': {name: quiz_count}, 'folders': {subject: {folder: count}}}
        Uses a single aggregation — no full quiz documents loaded."""
        subjects = {}
        folders = {}
        try:
            rows = self.mongo.aggregate('quizzes', [
                {'$group': {'_id': {'subject': '$subject', 'folder': '$folder'}, 'count': {'$sum': 1}}}
            ])
        except Exception:
            rows = []
        for row in rows or []:
            s = row['_id'].get('subject') or 'General'
            f = row['_id'].get('folder') or 'Uncategorized'
            subjects[s] = subjects.get(s, 0) + row['count']
            folders.setdefault(s, {})
            folders[s][f] = row['count']
        return {'subjects': subjects, 'folders': folders}
    
    def get_quizzes_by(self, subject, folder, active_only=True):
        """Get quizzes for a subject + folder"""
        query = {'subject': subject, 'folder': folder}
        if active_only:
            query['is_active'] = True
        return self.mongo.find('quizzes', query)
    
    def get_quiz_by_id(self, quiz_id_str):
        """Fetch a single quiz by its id (as string)"""
        try:
            return self.mongo.find_one('quizzes', {'_id': ObjectId(quiz_id_str)})
        except Exception:
            return None
    
    def set_admin_selection(self, context, subject, folder, subfolder=''):
        """Save the admin's selected subject/folder(/sub-folder) for quiz-saving mode"""
        context.user_data['add_state'] = {
            'subject': subject, 'folder': folder, 'subfolder': subfolder or '', 'saved_count': 0
        }
    
    def clear_admin_selection(self, context):
        """Clear the admin's selected subject/folder"""
        context.user_data['add_state'] = None
    
    def start_quiz_session(self, context, subject, folder, subfolder='', limit=None, timer_seconds=0):
        """Create a shuffled per-user quiz session.
        subfolder='' means all questions in the folder (any/no sub-folder).
        limit caps the number of questions; timer_seconds sets the per-question time limit.
        Returns the session dict, or None if there are no matching active questions."""
        query = {'subject': subject, 'folder': folder, 'is_active': True}
        if subfolder:
            query['subfolder'] = subfolder
        quizzes = self.mongo.find('quizzes', query)
        if not quizzes:
            return None
        ids = [str(q['_id']) for q in quizzes]
        random.shuffle(ids)
        if limit and limit > 0:
            ids = ids[:limit]
        session = {
            'subject': subject,
            'folder': folder,
            'subfolder': subfolder or '',   # NEW
            'remaining_quiz_ids': ids,
            'total_questions': len(ids),
            'current_question': 0,
            'score': 0,          # NEW: correct answers in this session
            'answered': 0,       # NEW: questions the user has answered
            'last_poll_id': None,       # NEW: poll id of the question on screen
            'last_correct_option_id': None,  # NEW: to grade the answer
            'timer_seconds': timer_seconds or 0,   # NEW: per-question time limit (0 = no limit)
        }
        context.user_data['quiz_session'] = session
        self.stats['user_quiz_sessions'] = self.stats.get('user_quiz_sessions', 0) + 1
        self.save_stats()
        return session
    
    def start_quiz_session_multi(self, context, subject, folders, limit=None, timer_seconds=0):
        """NEW: Like start_quiz_session, but pulls questions from MULTIPLE chapters/folders at once
        (all sub-folders included). Returns the session dict, or None if nothing matches."""
        query = {'subject': subject, 'folder': {'$in': list(folders)}, 'is_active': True}
        quizzes = self.mongo.find('quizzes', query)
        if not quizzes:
            return None
        ids = [str(q['_id']) for q in quizzes]
        random.shuffle(ids)
        if limit and limit > 0:
            ids = ids[:limit]
        session = {
            'subject': subject,
            'folder': None,
            'folders': list(folders),   # NEW: multiple chapters
            'is_multi': True,           # NEW: flag used by shared session code
            'subfolder': '',
            'remaining_quiz_ids': ids,
            'total_questions': len(ids),
            'current_question': 0,
            'score': 0,
            'answered': 0,
            'last_poll_id': None,
            'last_correct_option_id': None,
            'timer_seconds': timer_seconds or 0,
        }
        context.user_data['quiz_session'] = session
        self.stats['user_quiz_sessions'] = self.stats.get('user_quiz_sessions', 0) + 1
        self.save_stats()
        return session
    
    def start_quiz_session_subjmulti(self, context, subjects, limit=None, timer_seconds=0, folders=None, subfolders=None):
        """NEW: Like start_quiz_session_multi, but pools questions from MULTIPLE SUBJECTS
        (every folder/chapter under each selected subject). Returns the session dict,
        or None if nothing matches.
        folders/subfolders are OPTIONAL further narrowing (also picked via multi-select);
        None/empty means "no filter on that level" (all chapters / all sub-folders)."""
        query = {'subject': {'$in': list(subjects)}, 'is_active': True}
        if folders:
            query['folder'] = {'$in': list(folders)}
        if subfolders:
            query['subfolder'] = {'$in': list(subfolders)}
        quizzes = self.mongo.find('quizzes', query)
        if not quizzes:
            return None
        ids = [str(q['_id']) for q in quizzes]
        random.shuffle(ids)
        if limit and limit > 0:
            ids = ids[:limit]
        session = {
            'subject': None,
            'folder': None,
            'folders': list(folders) if folders else None,        # NEW: optional chapter narrowing
            'subfolders': list(subfolders) if subfolders else None,  # NEW: optional sub-folder narrowing
            'subjects': list(subjects),   # NEW: multiple subjects
            'is_multi': True,
            'is_subj_multi': True,        # NEW: flag used by shared session code
            'subfolder': '',
            'remaining_quiz_ids': ids,
            'total_questions': len(ids),
            'current_question': 0,
            'score': 0,
            'answered': 0,
            'last_poll_id': None,
            'last_correct_option_id': None,
            'timer_seconds': timer_seconds or 0,
        }
        context.user_data['quiz_session'] = session
        self.stats['user_quiz_sessions'] = self.stats.get('user_quiz_sessions', 0) + 1
        self.save_stats()
        return session
    
    def get_next_quiz_question(self, context):
        """Pop the next quiz for the active session.
        Returns (session, quiz) — quiz is None when the session is finished."""
        session = context.user_data.get('quiz_session')
        if not session:
            return None, None
        if not session.get('remaining_quiz_ids'):
            return session, None
        quiz_id = session['remaining_quiz_ids'].pop(0)
        session['current_question'] = session.get('current_question', 0) + 1
        quiz = self.get_quiz_by_id(quiz_id)
        if quiz is None:
            # Quiz was deleted mid-session → skip to the next one
            return self.get_next_quiz_question(context)
        return session, quiz
    
    # ==========================================================
    # NEW: MENU BUILDERS (shared by commands and callbacks)
    # ==========================================================
    
    def build_user_subject_menu(self):
        """(text, keyboard) for the user /quiz subject selection"""
        structure = self.get_structure()
        subjects = structure['subjects']
        if not subjects:
            return ("📚 Select a Subject:\n\n😔 No quiz subjects available yet.\nPlease check back later!",
                    [[InlineKeyboardButton("🔄 Refresh", callback_data="qz_back_subjects")]])
        text = "📚 Select a Subject:"
        keyboard = []
        for name in sorted(subjects.keys()):
            token = self.register_subject_token(name)
            keyboard.append([InlineKeyboardButton(f"📚 {name} ({subjects[name]})", callback_data=f"qz_subj_{token}")])
        # NEW: let the user pick more than one subject/quiz-list at once
        keyboard.append([InlineKeyboardButton(
            "☑️ Select Multiple Subjects", callback_data="qzsm_mode")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="qz_back_subjects")])
        return text, keyboard
    
    def build_user_subject_multi_menu(self, selected):
        """NEW: (text, keyboard) for picking MULTIPLE subjects/quiz-lists at once.
        Tapping a subject toggles its checkbox; nothing else changes on screen."""
        structure = self.get_structure()
        subjects = structure['subjects']
        n = len(selected)
        text = (f"☑️ Select Multiple Subjects\n\n"
                f"Tap subjects to select/unselect them. You can pick as many as you like.\n\n"
                f"✅ Selected: {n}")
        keyboard = []
        for name in sorted(subjects.keys()):
            checkbox = "☑️" if name in selected else "⬜"
            token = self.register_subject_token(name)
            keyboard.append([InlineKeyboardButton(
                f"{checkbox} {name} ({subjects[name]})", callback_data=f"qzsm_tgl_{token}")])
        control_row = [
            InlineKeyboardButton("✅ Select All", callback_data="qzsm_all"),
            InlineKeyboardButton("◻️ Clear All", callback_data="qzsm_clr")
        ]
        keyboard.append(control_row)
        if selected:
            keyboard.append([InlineKeyboardButton(
                f"➡️ Next ({n} selected)", callback_data="qzsm_start")])
        keyboard.append([InlineKeyboardButton("🔙 Single-Select Mode", callback_data="qz_back_subjects")])
        return text, keyboard
    
    def build_subjmulti_chapter_menu(self, subjects, selected):
        """NEW: (text, keyboard) for picking MULTIPLE chapters at once, across all of the
        already-selected subjects (folder names are a UNION across those subjects)."""
        folders = self.get_union_folders(subjects)
        subj_label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
        n = len(selected)
        if not folders:
            text = (f"☑️ Select Multiple Chapters\n\n📚 Subjects: {subj_label}\n\n"
                    f"😔 No chapters found under the selected subjects.")
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="qzsm_mode")]]
            return text, keyboard
        text = (f"☑️ Select Multiple Chapters\n\n📚 Subjects: {subj_label}\n\n"
                f"Tap chapters to select/unselect them. You can pick as many as you like.\n\n"
                f"✅ Selected: {n}")
        keyboard = []
        for name in sorted(folders.keys()):
            checkbox = "☑️" if name in selected else "⬜"
            token = self.register_name_token(name)
            keyboard.append([InlineKeyboardButton(
                f"{checkbox} {name} ({folders[name]})", callback_data=f"qzsmch_tgl_{token}")])
        control_row = [
            InlineKeyboardButton("✅ Select All", callback_data="qzsmch_all"),
            InlineKeyboardButton("◻️ Clear All", callback_data="qzsmch_clr")
        ]
        keyboard.append(control_row)
        if selected:
            keyboard.append([InlineKeyboardButton(
                f"➡️ Next ({n} selected)", callback_data="qzsmch_next")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="qzsm_mode")])
        return text, keyboard
    
    def build_subjmulti_subfolder_menu(self, subjects, folders, selected):
        """NEW: (text, keyboard) for picking MULTIPLE sub-folders at once, across all of the
        already-selected subjects × chapters (sub-folder names are a UNION across those)."""
        subfolder_names = self.get_union_subfolders(subjects, folders)
        subj_label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
        folder_label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
        n = len(selected)
        text = (f"☑️ Select Multiple Sub-folders\n\n📚 Subjects: {subj_label}\n📁 Chapters: {folder_label}\n\n"
                f"Tap sub-folders to select/unselect them.\n"
                f"Leave none selected to include ALL questions (with & without sub-folders).\n\n"
                f"✅ Selected: {n}")
        keyboard = []
        for name in subfolder_names:
            checkbox = "☑️" if name in selected else "⬜"
            token = self.register_name_token(name)
            cnt = self.mongo.count_documents('quizzes', {
                'subject': {'$in': list(subjects)}, 'folder': {'$in': list(folders)},
                'subfolder': name, 'is_active': True})
            keyboard.append([InlineKeyboardButton(
                f"{checkbox} {name} ({cnt})", callback_data=f"qzsmsf_tgl_{token}")])
        control_row = [
            InlineKeyboardButton("✅ Select All", callback_data="qzsmsf_all"),
            InlineKeyboardButton("◻️ Clear All", callback_data="qzsmsf_clr")
        ]
        keyboard.append(control_row)
        start_label = f"▶️ Start Quiz ({n} selected)" if n else "▶️ Start Quiz (All)"
        keyboard.append([InlineKeyboardButton(start_label, callback_data="qzsmsf_start")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="qzsmch_show")])
        return text, keyboard
    
    def build_subjmulti_full_count_menu(self, subjects, folders, subfolders, ctx_token, back_callback):
        """NEW: (text, keyboard) asking how many questions, for a full
        subjects × chapters × sub-folders selection."""
        query = {'subject': {'$in': list(subjects)}, 'is_active': True}
        if folders:
            query['folder'] = {'$in': list(folders)}
        if subfolders:
            query['subfolder'] = {'$in': list(subfolders)}
        total = self.mongo.count_documents('quizzes', query)
        subj_label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
        folder_label = ", ".join(folders) if folders and len(folders) <= 3 else (
            f"{len(folders)} chapters selected" if folders else "All")
        text = (f"❓ How many questions do you want?\n\n"
                f"📚 Subjects: {subj_label}\n📁 Chapters: {folder_label}\n")
        if subfolders:
            sf_label = ", ".join(subfolders) if len(subfolders) <= 3 else f"{len(subfolders)} sub-folders selected"
            text += f"📂 Sub-folders: {sf_label}\n"
        text += f"📝 Available: {total} question(s)\n\nChoose an option below (or send a custom number):"
        keyboard = []
        row = []
        for preset in (10, 20, 50, 100):
            if preset < total:
                row.append(InlineKeyboardButton(str(preset), callback_data=f"qzsmf_cnt_{ctx_token}_{preset}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(f"📚 All ({total})", callback_data=f"qzsmf_cnt_{ctx_token}_{total}")])
        keyboard.append([InlineKeyboardButton("✏️ Custom Number", callback_data=f"qzsmf_cntcustom_{ctx_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_callback)])
        return text, keyboard
    
    def build_subjmulti_full_timer_menu(self, ctx_token, count):
        """NEW: (text, keyboard) asking for the per-question time limit, for a full
        subjects × chapters × sub-folders quiz."""
        text = (f"⏱ Set a Timer\n\n"
                f"📝 Questions selected: {count}\n\n"
                f"Choose how much time you get per question:")
        keyboard = []
        row = []
        for secs, label in ((10, "10 sec"), (20, "20 sec"), (30, "30 sec"), (45, "45 sec"), (60, "1 min")):
            row.append(InlineKeyboardButton(label, callback_data=f"qzsmf_tmr_{ctx_token}_{count}_{secs}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✏️ Custom Timer", callback_data=f"qzsmf_tmrcustom_{ctx_token}_{count}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qzsmf_backcnt_{ctx_token}")])
        return text, keyboard
    
    def build_subjmulti_count_menu(self, subjects, ctx_token):
        """NEW: (text, keyboard) asking how many questions, for a multi-SUBJECT selection."""
        query = {'subject': {'$in': list(subjects)}, 'is_active': True}
        total = self.mongo.count_documents('quizzes', query)
        label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
        text = (f"❓ How many questions do you want?\n\n"
                f"📚 Subjects: {label}\n📝 Available: {total} question(s)\n\n"
                f"Choose an option below (or send a custom number):")
        keyboard = []
        row = []
        for preset in (10, 20, 50, 100):
            if preset < total:
                row.append(InlineKeyboardButton(str(preset), callback_data=f"qzsc_cnt_{ctx_token}_{preset}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(f"📚 All ({total})", callback_data=f"qzsc_cnt_{ctx_token}_{total}")])
        keyboard.append([InlineKeyboardButton("✏️ Custom Number", callback_data=f"qzsc_cntcustom_{ctx_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="qzsm_mode")])
        return text, keyboard
    
    def build_subjmulti_timer_menu(self, ctx_token, count):
        """NEW: (text, keyboard) asking for the per-question time limit, for a multi-SUBJECT quiz."""
        text = (f"⏱ Set a Timer\n\n"
                f"📝 Questions selected: {count}\n\n"
                f"Choose how much time you get per question:")
        keyboard = []
        row = []
        for secs, label in ((10, "10 sec"), (20, "20 sec"), (30, "30 sec"), (45, "45 sec"), (60, "1 min")):
            row.append(InlineKeyboardButton(label, callback_data=f"qzst_tmr_{ctx_token}_{count}_{secs}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✏️ Custom Timer", callback_data=f"qzst_tmrcustom_{ctx_token}_{count}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qzsc_backcnt_{ctx_token}")])
        return text, keyboard
    
    def build_user_folder_menu(self, subject):
        """(text, keyboard) for the user /quiz folder selection"""
        folders = self.get_structure()['folders'].get(subject, {})
        if not folders:
            return (f"📁 {subject} Quizzes:\n\n😔 No quiz folders under this subject yet.\nPlease check back later!",
                    [[InlineKeyboardButton("🔙 Back to Subjects", callback_data="qz_back_subjects")]])
        text = f"📁 {subject} Quizzes:"
        keyboard = []
        for name in sorted(folders.keys()):
            token = self.register_pair_token(subject, name)
            keyboard.append([InlineKeyboardButton(f"📁 {name} ({folders[name]})", callback_data=f"qz_fold_{token}")])
        # NEW: let the user pick more than one chapter/folder at once
        if folders:
            subject_token = self.register_subject_token(subject)
            keyboard.append([InlineKeyboardButton(
                "☑️ Select Multiple Chapters", callback_data=f"qzm_mode_{subject_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="qz_back_subjects")])
        return text, keyboard
    
    def build_user_folder_multi_menu(self, subject, selected):
        """NEW: (text, keyboard) for picking MULTIPLE chapters/folders at once.
        Tapping a chapter toggles its checkbox; nothing else changes on screen."""
        folders = self.get_structure()['folders'].get(subject, {})
        subject_token = self.register_subject_token(subject)
        n = len(selected)
        text = (f"☑️ {subject} — Select Multiple Chapters\n\n"
                f"Tap chapters to select/unselect them. You can pick as many as you like.\n\n"
                f"✅ Selected: {n}")
        keyboard = []
        for name in sorted(folders.keys()):
            checkbox = "☑️" if name in selected else "⬜"
            token = self.register_pair_token(subject, name)
            keyboard.append([InlineKeyboardButton(
                f"{checkbox} {name} ({folders[name]})", callback_data=f"qzm_tgl_{token}")])
        control_row = [
            InlineKeyboardButton("✅ Select All", callback_data=f"qzm_all_{subject_token}"),
            InlineKeyboardButton("◻️ Clear All", callback_data=f"qzm_clr_{subject_token}")
        ]
        keyboard.append(control_row)
        if selected:
            keyboard.append([InlineKeyboardButton(
                f"▶️ Start Quiz ({n} selected)", callback_data=f"qzm_start_{subject_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Single-Select Mode", callback_data="qz_back_folders")])
        keyboard.append([InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")])
        return text, keyboard
    
    def build_multi_quiz_count_menu(self, subject, folders, ctx_token):
        """NEW: (text, keyboard) asking how many questions, for a multi-chapter selection."""
        query = {'subject': subject, 'folder': {'$in': list(folders)}, 'is_active': True}
        total = self.mongo.count_documents('quizzes', query)
        label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
        text = (f"❓ How many questions do you want?\n\n"
                f"📚 Subject: {subject}\n📁 Chapters: {label}\n📝 Available: {total} question(s)\n\n"
                f"Choose an option below (or send a custom number):")
        keyboard = []
        row = []
        for preset in (10, 20, 50, 100):
            if preset < total:
                row.append(InlineKeyboardButton(str(preset), callback_data=f"qzm_cnt_{ctx_token}_{preset}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(f"📚 All ({total})", callback_data=f"qzm_cnt_{ctx_token}_{total}")])
        keyboard.append([InlineKeyboardButton("✏️ Custom Number", callback_data=f"qzm_cntcustom_{ctx_token}")])
        subject_token = self.register_subject_token(subject)
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qzm_mode_{subject_token}")])
        return text, keyboard
    
    def build_multi_quiz_timer_menu(self, ctx_token, count):
        """NEW: (text, keyboard) asking for the per-question time limit, for a multi-chapter quiz."""
        text = (f"⏱ Set a Timer\n\n"
                f"📝 Questions selected: {count}\n\n"
                f"Choose how much time you get per question:")
        keyboard = []
        row = []
        for secs, label in ((10, "10 sec"), (20, "20 sec"), (30, "30 sec"), (45, "45 sec"), (60, "1 min")):
            row.append(InlineKeyboardButton(label, callback_data=f"qzm_tmr_{ctx_token}_{count}_{secs}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✏️ Custom Timer", callback_data=f"qzm_tmrcustom_{ctx_token}_{count}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qzm_backcnt_{ctx_token}")])
        return text, keyboard
    
    def build_user_subfolder_menu(self, subject, folder):
        """NEW: (text, keyboard) for the user's sub-folder selection screen (only shown when
        the folder actually has sub-folders)"""
        subfolders = self.get_subfolders(subject, folder)
        total = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'is_active': True})
        text = (f"📂 {folder} ({subject})\n\n"
                f"This quiz folder has sub-folders. Pick one, or play everything in the folder:")
        keyboard = []
        for name in subfolders:
            cnt = self.mongo.count_documents(
                'quizzes', {'subject': subject, 'folder': folder, 'subfolder': name, 'is_active': True})
            token = self.register_qz_ctx(subject, folder, name)
            keyboard.append([InlineKeyboardButton(f"📂 {name} ({cnt})", callback_data=f"qz_subf_{token}")])
        all_token = self.register_qz_ctx(subject, folder, '')
        keyboard.append([InlineKeyboardButton(f"📄 All Questions in Folder ({total})", callback_data=f"qz_subf_{all_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="qz_back_folders")])
        return text, keyboard
    
    def build_user_folder_start(self, subject, folder, subfolder=''):
        """(text, keyboard) for the user's 'Start Quiz' confirm screen"""
        query = {'subject': subject, 'folder': folder, 'is_active': True}
        if subfolder:
            query['subfolder'] = subfolder
        count = self.mongo.count_documents('quizzes', query)
        token = self.register_qz_ctx(subject, folder, subfolder)
        label = f"{folder} → 📂 {subfolder}" if subfolder else folder
        text = (f"🎯 Quiz Selected\n\n"
                f"📚 Subject: {subject}\n"
                f"📁 Quiz Folder: {label}\n"
                f"📝 Questions available: {count}\n\n"
                f"Questions will be sent in random order.\n"
                f"Each question appears only once per session.")
        keyboard = [
            [InlineKeyboardButton("▶️ Start Quiz", callback_data=f"qz_pickcount_{token}")],
            [InlineKeyboardButton("🔙 Back", callback_data="qz_back_folders")]
        ]
        return text, keyboard
    
    def build_quiz_count_menu(self, subject, folder, subfolder, ctx_token):
        """NEW: (text, keyboard) asking how many questions the user wants."""
        query = {'subject': subject, 'folder': folder, 'is_active': True}
        if subfolder:
            query['subfolder'] = subfolder
        total = self.mongo.count_documents('quizzes', query)
        label = f"{folder} → 📂 {subfolder}" if subfolder else folder
        text = (f"❓ How many questions do you want?\n\n"
                f"📚 Subject: {subject}\n📁 {label}\n📝 Available: {total} question(s)\n\n"
                f"Choose an option below (or send a custom number):")
        keyboard = []
        row = []
        for preset in (10, 20, 50, 100):
            if preset < total:
                row.append(InlineKeyboardButton(str(preset), callback_data=f"qz_cnt_{ctx_token}_{preset}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(f"📚 All ({total})", callback_data=f"qz_cnt_{ctx_token}_{total}")])
        keyboard.append([InlineKeyboardButton("✏️ Custom Number", callback_data=f"qz_cntcustom_{ctx_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qz_subf_{ctx_token}")])
        return text, keyboard
    
    def build_quiz_timer_menu(self, ctx_token, count):
        """NEW: (text, keyboard) asking for the per-question time limit."""
        text = (f"⏱ Set a Timer\n\n"
                f"📝 Questions selected: {count}\n\n"
                f"Choose how much time you get per question:")
        keyboard = []
        row = []
        for secs, label in ((10, "10 sec"), (20, "20 sec"), (30, "30 sec"), (45, "45 sec"), (60, "1 min")):
            row.append(InlineKeyboardButton(label, callback_data=f"qz_tmr_{ctx_token}_{count}_{secs}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✏️ Custom Timer", callback_data=f"qz_tmrcustom_{ctx_token}_{count}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"qz_pickcount_{ctx_token}")])
        return text, keyboard
    
    def build_admin_subject_menu(self):
        """(text, keyboard) for the admin 'Add Quiz — Step 1' screen"""
        structure = self.get_structure()
        subjects = structure['subjects']
        text = ("📝 Add Quiz — Step 1: Select Subject\n\n"
                "Choose an existing subject or create a new one.\n\n"
                "Flow: Subject → Quiz Folder → send Quiz Mode polls.\n\n"
                "💡 How to create a Quiz Mode poll:\n"
                "1. Tap the 📎 attachment icon → Poll\n"
                "2. Enter question and options\n"
                "3. ✅ Enable Quiz Mode and set the correct answer\n"
                "4. Send it to me")
        keyboard = []
        for name in sorted(subjects.keys()):
            token = self.register_subject_token(name)
            keyboard.append([InlineKeyboardButton(f"📚 {name} ({subjects[name]})", callback_data=f"addquiz_subj_{token}")])
        keyboard.append([InlineKeyboardButton("➕ Create New Subject", callback_data="addquiz_newsubj")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="start_menu")])
        return text, keyboard
    
    def build_admin_folder_menu(self, subject):
        """(text, keyboard) for the admin 'Add Quiz — Step 2' screen"""
        folders = self.get_structure()['folders'].get(subject, {})
        text = (f"📁 Add Quiz — Step 2: Select Quiz Folder\n\n"
                f"📚 Subject: {subject}\n\n"
                f"Choose a quiz folder or create a new one:")
        keyboard = []
        for name in sorted(folders.keys()):
            token = self.register_pair_token(subject, name)
            keyboard.append([InlineKeyboardButton(f"📁 {name} ({folders[name]})", callback_data=f"addquiz_fold_{token}")])
        keyboard.append([InlineKeyboardButton("➕ Create New Quiz Folder", callback_data="addquiz_newfold")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="addquiz_backsubj")])
        return text, keyboard
    
    def build_admin_subfolder_menu(self, subject, folder):
        """NEW: (text, keyboard) for the admin 'Add Quiz — Step 3: Sub-folder (optional)' screen"""
        subfolders = self.get_subfolders(subject, folder)
        text = (f"📂 Add Quiz — Step 3: Sub-folder (optional)\n\n"
                f"📚 Subject: {subject}\n📁 Quiz Folder: {folder}\n\n"
                f"Pick an existing sub-folder, add directly to this folder, "
                f"or create a new sub-folder:")
        keyboard = []
        for name in subfolders:
            cnt = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'subfolder': name})
            token = self.register_qz_ctx(subject, folder, name)
            keyboard.append([InlineKeyboardButton(f"📂 {name} ({cnt})", callback_data=f"addquiz_subf_{token}")])
        root_token = self.register_pair_token(subject, folder)
        keyboard.append([InlineKeyboardButton("📄 Add Directly to This Folder", callback_data=f"addquiz_nosubf_{root_token}")])
        keyboard.append([InlineKeyboardButton("➕ Create New Sub-folder", callback_data=f"addquiz_newsubf_{root_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="addquiz_backsubj")])
        return text, keyboard
    
    def load_quizzes(self):
        """Load quizzes from MongoDB"""
        return self.mongo.find('quizzes')
    
    def load_groups(self):
        """Load groups from MongoDB"""
        return self.mongo.find('groups')
    
    def load_settings(self):
        """Load settings from MongoDB"""
        settings = self.mongo.find_one('settings', {'_id': 'bot_settings'})
        if not settings:
            # Default settings
            settings = {
                '_id': 'bot_settings',
                'quiz_interval': 3600,  # 1 hour in seconds
                'quiz_explanation': "Check back later for results!",
                'max_quizzes_per_day': 24,
                'auto_clean_inactive': True,
                'inactive_days_threshold': 7,
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat()
            }
            self.mongo.insert_one('settings', settings)
        return settings
    
    def load_stats(self):
        """Load stats from MongoDB"""
        stats = self.mongo.find_one('stats', {'_id': 'bot_stats'})
        if not stats:
            # Default stats
            stats = {
                '_id': 'bot_stats',
                'total_quizzes_sent': 0,
                'total_groups_reached': 0,
                'quizzes_added': 0,
                'bot_start_time': datetime.now().isoformat(),
                'last_quiz_sent': None,
                'group_engagement': {},
                'total_broadcasts_sent': 0,
                'manual_quizzes_sent': 0,
                'quiz_reports_received': 0,
                'quizzes_deleted_by_reports': 0
            }
            self.mongo.insert_one('stats', stats)
        return stats
    
    def load_sudo_users(self):
        """Load additional sudo users from MongoDB"""
        docs = self.mongo.find('sudo_users', {})
        return {doc['user_id'] for doc in docs}
    
    def save_sudo_user(self, user_id):
        """Save a sudo user to MongoDB"""
        if not self.mongo.find_one('sudo_users', {'user_id': user_id}):
            self.mongo.insert_one('sudo_users', {'user_id': user_id})
            self.sudo_users.add(user_id)
    
    def remove_sudo_user(self, user_id):
        """Remove a sudo user from MongoDB"""
        self.mongo.delete_one('sudo_users', {'user_id': user_id})
        self.sudo_users.discard(user_id)
    
    def is_admin(self, user_id):
        """Check if user is bot admin (main admin or sudo user)"""
        return user_id == ADMIN_USER_ID or user_id in self.sudo_users
        
    def save_quiz(self, quiz):
        """Save quiz to MongoDB"""
        if '_id' in quiz:
            self.mongo.replace_one('quizzes', {'_id': quiz['_id']}, quiz)
        else:
            result = self.mongo.insert_one('quizzes', quiz)
            if result and result.inserted_id:
                quiz['_id'] = result.inserted_id
    
    def save_group(self, group):
        """Save group to MongoDB"""
        if '_id' in group:
            self.mongo.replace_one('groups', {'_id': group['_id']}, group)
        else:
            result = self.mongo.insert_one('groups', group)
            if result and result.inserted_id:
                group['_id'] = result.inserted_id
    
    def save_settings(self):
        """Save settings to MongoDB"""
        self.settings['updated_at'] = datetime.now().isoformat()
        self.mongo.replace_one('settings', {'_id': 'bot_settings'}, self.settings)
    
    def save_stats(self):
        """Save stats to MongoDB"""
        self.mongo.replace_one('stats', {'_id': 'bot_stats'}, self.stats)

    def get_random_quiz(self, exclude_recent_count=8, candidates=None):
        """Get a random quiz that hasn't been sent recently - IMPROVED ANTI-REPEAT
        candidates: optional list restricting the pool (e.g. subject/folder filtered).
        Default pool is ALL quizzes from ALL subjects and folders."""
        pool = self.quizzes if candidates is None else candidates
        if not pool:
            return None
        
        # Get active quizzes only
        active_quizzes = [q for q in pool if q.get('is_active', True)]
        if not active_quizzes:
            return None
        
        print(f"🔍 Available quizzes: {len(active_quizzes)}, Recently sent: {len(self.recently_sent_quizzes)}")
        
        # If we have very few quizzes, just return a random one
        if len(active_quizzes) <= 3:
            quiz = random.choice(active_quizzes)
            print(f"📝 Few quizzes available, selected: {quiz['question'][:50]}...")
            return quiz
        
        # Remove old entries from recently_sent_quizzes if it gets too large
        if len(self.recently_sent_quizzes) > self.max_recent_track:
            self.recently_sent_quizzes = self.recently_sent_quizzes[-self.max_recent_track:]
        
        # Get quizzes that haven't been sent recently
        available_quizzes = [q for q in active_quizzes if q['_id'] not in self.recently_sent_quizzes]
        
        # If no available quizzes (all were sent recently), use least recently sent
        if not available_quizzes:
            print("🔄 All quizzes recently sent, using least recent ones")
            # Sort by last_sent date (oldest first)
            available_quizzes = sorted(
                active_quizzes,
                key=lambda x: x.get('last_sent', '2000-01-01')
            )
        
        # If still no quizzes, return random
        if not available_quizzes:
            quiz = random.choice(active_quizzes)
        else:
            quiz = random.choice(available_quizzes)
        
        print(f"🎯 Selected quiz: {quiz['question'][:50]}...")
        return quiz

    def track_recent_quiz(self, quiz_id):
        """Track a quiz as recently sent"""
        if quiz_id in self.recently_sent_quizzes:
            self.recently_sent_quizzes.remove(quiz_id)
        self.recently_sent_quizzes.append(quiz_id)
        
        # Keep only recent ones
        if len(self.recently_sent_quizzes) > self.max_recent_track:
            self.recently_sent_quizzes = self.recently_sent_quizzes[-self.max_recent_track:]

    async def ensure_group_registered(self, chat_id, chat_title=None):
        """Ensure a group is registered in the database"""
        # Don't register private chats (admin's private chat)
        if chat_id > 0:  # Positive IDs are user IDs, negative are group IDs
            print(f"⚠️ Skipping private chat registration: {chat_id}")
            return None
            
        existing_group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        if not existing_group:
            # Register the group
            group_info = {
                'chat_id': chat_id,
                'title': chat_title or f"Group {chat_id}",
                'added_date': datetime.now().isoformat(),
                'member_count': 0,
                'quizzes_received': 0,
                'manual_quizzes_received': 0,
                'last_activity': datetime.now().isoformat(),
                'is_active': True
            }
            self.mongo.insert_one('groups', group_info)
            self.groups = self.load_groups()  # Reload groups
            print(f"✅ Auto-registered group: {chat_title or chat_id}")
        
        return self.mongo.find_one('groups', {'chat_id': chat_id})
    
    def parse_time_input(self, time_str):
        """Parse time input with various formats (2h, 30m, 1.5h, 90m, etc.)"""
        time_str = time_str.lower().strip()
        
        # Regex to match numbers and units
        match = re.match(r'^(\d*\.?\d+)\s*([hm]|min|hr|hour|minute)?$', time_str)
        if not match:
            return None
        
        value = float(match.group(1))
        unit = match.group(2) or 'h'  # Default to hours if no unit specified
        
        # Convert to seconds
        if unit in ['m', 'min', 'minute']:
            return int(value * 60)  # minutes to seconds
        elif unit in ['h', 'hr', 'hour']:
            return int(value * 3600)  # hours to seconds
        else:
            return None
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type
        
        if chat_type == 'private':
            if self.is_admin(user_id):
                keyboard = [
                    [InlineKeyboardButton("📊 View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")],
                    [InlineKeyboardButton("🗂 Manage Quiz Folders", callback_data="manage_folders")],
                    [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
                    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
                    [InlineKeyboardButton("👥 Manage Groups", callback_data="manage_groups")],
                    [InlineKeyboardButton("📋 Export Data", callback_data="export_data")],
                    [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
                    [InlineKeyboardButton("⚠️ View Reports", callback_data="view_reports")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                quiz_interval_hours = self.quiz_interval / 3600
                
                # Works from both /start command and 🏠 Main Menu callback buttons
                # (update.message is None for callback query updates)
                reply_target = update.message or (update.callback_query.message if update.callback_query else None)
                await reply_target.reply_text(
                    f"👋 **Admin Dashboard**\n\n"
                    f"I'm your Quiz Bot! Choose an option below:\n\n"
                    f"📊 **Statistics** - View detailed bot analytics\n"
                    f"📝 **Add Quiz** - Select Subject → Quiz Folder, then send QUIZ MODE polls\n"
                    f"🗂 **Manage Quiz Folders** - View/create/rename/delete subjects and folders\n"
                    f"⚙️ **Settings** - Configure bot settings (Current: {quiz_interval_hours}h interval)\n"
                    f"📢 **Broadcast** - Send message to all groups\n"
                    f"👥 **Manage Groups** - View and manage groups\n"
                    f"📋 **Export Data** - Export quizzes and stats\n"
                    f"🔄 **Reset Quizzes** - Delete all saved quizzes\n"
                    f"⚠️ **View Reports** - Check reported quizzes\n\n"
                    f"To add a quiz: 📝 Add Quiz → Subject → Folder → send Quiz Mode polls → /done",
                    reply_markup=reply_markup
                )
            else:
                bot_username = context.bot.username
                keyboard = [
                    [InlineKeyboardButton("➕ Add me to your Group", url=f"https://t.me/{bot_username}?startgroup=true")],
                    [InlineKeyboardButton("🎮 Play Quiz", callback_data="qz_back_subjects")]
                ]
                if SUPPORT_USERNAME:
                    support_url = SUPPORT_USERNAME if SUPPORT_USERNAME.startswith('http') else f"https://t.me/{SUPPORT_USERNAME}"
                    keyboard.append([InlineKeyboardButton("🆘 Support", url=support_url)])
                await update.message.reply_text(
                    "👋 **Welcome!**\n\n"
                    "I'm a Quiz Bot — I send fun quiz polls in groups and let you play quizzes right here in DM!\n\n"
                    "➕ **Add me to your group** and make me an admin to start receiving quiz polls automatically.\n\n"
                    "⚡ **Group Commands:**\n"
                    "• /quiz - Browse and start a quiz (group admins)\n"
                    "• /rquiz - Send an immediate random quiz (group admins)\n"
                    "• /qreport - Report a quiz for review (reply to a quiz with this command)\n\n"
                    "🎮 **Play Quizzes:**\n"
                    "• /quiz - Browse subjects and quiz folders, then play right here in private chat!\n"
                    "• /stop - End your running quiz and see your score (next question comes automatically after each answer!)\n\n"
                    "Need help? Tap 🆘 Support below.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        else:
            # Bot added to a group - only for groups and supergroups
            if chat_type in ['group', 'supergroup']:
                await self.add_to_group(update)
            else:
                # For channels or other chat types
                await update.message.reply_text(
                    "⚠️ This bot is designed for groups and supergroups only.\n"
                    "Please add me to a group to start receiving quizzes!"
                )
    
    async def add_to_group(self, update: Update):
        """Handle bot being added to a group"""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title
        
        # Check if group already exists in MongoDB
        existing_group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        group_info = {
            'chat_id': chat_id,
            'title': chat_title,
            'added_date': datetime.now().isoformat(),
            'member_count': update.effective_chat.get_member_count() if update.effective_chat.get_member_count else 0,
            'quizzes_received': existing_group['quizzes_received'] if existing_group else 0,
            'manual_quizzes_received': existing_group['manual_quizzes_received'] if existing_group else 0,
            'last_activity': datetime.now().isoformat(),
            'is_active': True
        }
        
        if existing_group:
            # Update existing group
            group_info['_id'] = existing_group['_id']
            self.mongo.replace_one('groups', {'_id': existing_group['_id']}, group_info)
            message = f"🎉 I'm back in {chat_title}! I'll continue sending quiz polls.\n\nUse /rquiz to send an immediate quiz!"
        else:
            # Add new group
            self.mongo.insert_one('groups', group_info)
            message = f"🎉 Thanks for adding me to {chat_title}!\n\nI'll send random quiz polls automatically!\n\nUse /rquiz to send an immediate quiz!"
        
        # Reload groups from MongoDB
        self.groups = self.load_groups()
        
        # Send welcome message with group controls for admin
        if self.is_admin(update.effective_user.id):
            keyboard = [
                [InlineKeyboardButton("🚫 Remove from Group", callback_data=f"remove_group_{chat_id}")],
                [InlineKeyboardButton("📊 Group Stats", callback_data=f"group_stats_{chat_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message)
    
    async def handle_private_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle private messages from admin"""
        user_id = update.effective_user.id
        
        # NEW: quiz custom-count / custom-timer text input is available to ANY user
        # (not just admin) since /quiz is open to everyone.
        if context.user_data.get('await') in (
                'quiz_custom_count', 'quiz_custom_timer',
                'quiz_multi_custom_count', 'quiz_multi_custom_timer',
                'quiz_subjmulti_custom_count', 'quiz_subjmulti_custom_timer',
                'quiz_subjmulti_full_custom_count', 'quiz_subjmulti_full_custom_timer'):
            await self.handle_quiz_custom_input(update, context)
            return
        
        if not self.is_admin(user_id):
            await update.message.reply_text("I only accept commands from the admin.")
            return
        
        # Check if user is in broadcast mode
        if self.broadcast_mode.get(user_id):
            await self.send_broadcast(update, context, update.message.text)
            return
        
        # Check if user is setting explanation
        if context.user_data.get('waiting_for_explanation'):
            await self.handle_explanation_input(update, context)
            return
        
        # Check if user is setting interval
        if context.user_data.get('waiting_for_interval'):
            await self.handle_interval_input(update, context)
            return
        
        # NEW: admin is sending the new question text for "Edit Question Text Only"
        if context.user_data.get('await') == 'edit_quiz_question':
            await self.handle_edit_quiz_question_input(update, context)
            return
        
        # NEW: handle incoming polls with the hierarchical flow
        if update.message.poll:
            # NEW: admin is sending a replacement poll for "Replace Entire Quiz"
            if context.user_data.get('quiz_edit_replace_pending'):
                await self.handle_edit_quiz_replace_poll(update, context, update.message.poll)
                return
            add_state = context.user_data.get('add_state') or {}
            if add_state.get('subject') and add_state.get('folder'):
                # Quiz-saving mode active → save under the selected subject/folder(/sub-folder)
                await self.save_poll_quiz(
                    update, context, update.message.poll,
                    add_state['subject'], add_state['folder'], add_state.get('subfolder', ''))
            else:
                # No location selected → do NOT silently save; guide the admin
                await update.message.reply_text(
                    "⚠️ Please select where to save the quiz first!\n\n"
                    "Tap 📝 Add Quiz below (or /start → 📝 Add Quiz), then choose:\n"
                    "Subject → Quiz Folder\n\n"
                    "After that, every Quiz Mode poll you send will automatically "
                    "be saved under your selection.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")]])
                )
            return
        
        # NEW: text-input states (new subject / new folder / renames)
        if context.user_data.get('await'):
            await self.handle_awaiting_input(update, context)
            return
        
        # Fallback help text
        await update.message.reply_text(
            "❓ I didn't understand that.\n\n"
            "📝 To add quizzes: /start → 📝 Add Quiz → select Subject → Quiz Folder, "
            "then send Quiz Mode polls. Finish with /done.\n\n"
            "💡 How to create a Quiz Mode poll:\n"
            "1. Tap the 📎 attachment icon → Poll\n"
            "2. Enter your question and options\n"
            "3. ✅ Enable 'Quiz Mode' and set the correct answer\n"
            "4. Send it to me"
        )
    
    async def save_poll_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE, poll, subject, folder, subfolder=''):
        """Save a poll as a quiz under the selected subject/folder(/sub-folder).
        Accepts BOTH anonymous and non-anonymous QUIZ MODE polls."""
        # Check if it's a quiz mode poll (has correct_option_id)
        if poll.correct_option_id is None:
            await update.message.reply_text(
                "❌ This is a regular poll, not a quiz!\n\n"
                "I only accept **QUIZ MODE** polls that have a correct answer set.\n\n"
                "Please create a new poll and make sure to:\n"
                "1. Enable 'Quiz Mode'\n"
                "2. Set the correct answer\n"
                "3. Then send it to me\n\n"
                "📝 I accept both anonymous and non-anonymous QUIZ MODE polls!"
            )
            return
        
        quiz = {
            'type': 'quiz',
            'subject': subject,
            'folder': folder,
            'subfolder': subfolder or '',
            'question': poll.question,
            'options': [option.text for option in poll.options],
            'is_anonymous': poll.is_anonymous,  # Keep original setting for reference
            'allows_multiple_answers': False,  # Quiz mode doesn't allow multiple answers
            'correct_option_id': poll.correct_option_id,
            'added_date': datetime.now().isoformat(),
            'sent_count': 0,
            'manual_sent_count': 0,
            'last_sent': None,
            'engagement': 0,
            'is_active': True
        }
        
        self.mongo.insert_one('quizzes', quiz)
        self.stats['quizzes_added'] += 1
        self.save_stats()
        
        # Reload quizzes from MongoDB
        self.quizzes = self.load_quizzes()
        
        # Track how many were saved in this adding session
        add_state = context.user_data.get('add_state') or {}
        add_state['saved_count'] = add_state.get('saved_count', 0) + 1
        context.user_data['add_state'] = add_state
        
        correct_answer = quiz['options'][quiz['correct_option_id']]
        folder_count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder})
        folder_label = f"{folder} → 📂 {subfolder}" if subfolder else folder
        
        await update.message.reply_text(
            f"✅ Quiz Saved!\n\n"
            f"📚 Subject: {subject}\n"
            f"📁 Quiz Folder: {folder_label}\n\n"
            f"📝 Question: {quiz['question']}\n"
            f"✅ Correct Answer: {correct_answer}\n\n"
            f"📊 Saved this session: {add_state['saved_count']}\n"
            f"📁 Questions in this folder (total): {folder_count}\n"
            f"📊 Total quizzes: {len(self.quizzes)}\n\n"
            f"➡️ Keep sending Quiz Mode polls, or send /done to finish.\n"
            f"💡 Group admins can use /rquiz to send immediate quizzes!\n"
            f"⚠️ Users can report quizzes with /qreport command"
        )
    
    async def send_random_quiz(self):
        """Send a random quiz poll to all groups (from ALL subjects and folders)"""
        if not self.quizzes or not self.groups:
            print("❌ No quizzes or groups available")
            return
        
        # Get a random quiz that hasn't been sent recently
        quiz = self.get_random_quiz(exclude_recent_count=8)  # Avoid last 8 sent quizzes
        
        if not quiz:
            print("❌ No quiz selected")
            return
        
        # Update quiz stats
        quiz['sent_count'] = quiz.get('sent_count', 0) + 1
        quiz['last_sent'] = datetime.now().isoformat()
        self.save_quiz(quiz)
        
        # Track as recently sent
        self.track_recent_quiz(quiz['_id'])
        
        # Update global stats
        self.stats['total_quizzes_sent'] += len(self.groups)
        self.stats['last_quiz_sent'] = datetime.now().isoformat()
        self.save_stats()
        
        sent_to = 0
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        
        print(f"📤 Sending quiz to {len(active_groups)} active groups: {quiz['question'][:50]}...")
        
        for group in active_groups:
            try:
                # Don't send quizzes to the admin's private chat (positive chat_id)
                if group['chat_id'] > 0:
                    print(f"⚠️ Skipping private chat (admin): {group['chat_id']}")
                    continue
                    
                await self.send_quiz_to_group(group, quiz)
                sent_to += 1
                await asyncio.sleep(0.5)  # Rate limiting
                
            except Exception as e:
                print(f"❌ Failed to send to group {group['chat_id']}: {e}")
                # Mark group as inactive if sending fails repeatedly
                group['is_active'] = False
                self.save_group(group)
        
        # Reload groups and stats after updates
        self.groups = self.load_groups()
        self.save_stats()
        
        print(f"✅ Sent quiz '{quiz['question'][:30]}...' to {sent_to}/{len(active_groups)} groups at {datetime.now()}")
        print(f"📊 Recent quizzes tracking: {len(self.recently_sent_quizzes)} quizzes")
    
    async def send_quiz_to_group(self, group, quiz):
        """Send a quiz to a specific group - ALWAYS NON-ANONYMOUS"""
        explanation = self.settings.get('quiz_explanation', "Check back later for results!")
        
        if quiz['type'] == 'quiz':
            # Send as QUIZ MODE poll with NON-ANONYMOUS voting (ALWAYS)
            message = await self.application.bot.send_poll(
                chat_id=group['chat_id'],
                question=f"🎯 Quiz Time: {quiz['question']}",
                options=quiz['options'],
                is_anonymous=False,  # ALWAYS force non-anonymous voting
                allows_multiple_answers=False,  # Quiz mode doesn't allow multiple answers
                type=Poll.QUIZ,  # Always QUIZ mode
                correct_option_id=quiz['correct_option_id'],
                explanation=explanation,
                open_period=0,  # No time limit,
                protect_content=False  # Allow forwarding
            )
        
        # Update group stats
        group['quizzes_received'] = group.get('quizzes_received', 0) + 1
        group['last_activity'] = datetime.now().isoformat()
        self.save_group(group)
        
        # NEW: remember which DB quiz this poll came from, so /qreport can find it precisely
        self.poll_quiz_map[f"{group['chat_id']}:{message.message_id}"] = str(quiz['_id'])
        
        # Track engagement
        if str(group['chat_id']) not in self.stats['group_engagement']:
            self.stats['group_engagement'][str(group['chat_id'])] = 0
        self.stats['group_engagement'][str(group['chat_id'])] += 1
    
    async def send_immediate_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rquiz command - send immediate random quiz to current group.
        Optional filters: /rquiz <Subject> or /rquiz <Subject> <Quiz Folder>"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        chat_title = update.effective_chat.title
        
        # Check if it's a group chat
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command can only be used in groups!")
            return
        
        # Don't send quizzes to admin's private chat
        if chat_id > 0:  # Positive IDs are user IDs
            await update.message.reply_text("❌ This command can only be used in groups!")
            return
        
        # Check if user is admin of the group or bot admin
        is_admin = False
        
        # Check if user is bot admin
        if self.is_admin(user_id):
            is_admin = True
        else:
            # Check if user is admin in the group
            try:
                chat_member = await context.bot.get_chat_member(chat_id, user_id)
                if chat_member.status in ['administrator', 'creator']:
                    is_admin = True
            except Exception as e:
                print(f"Error checking admin status: {e}")
        
        if not is_admin:
            await update.message.reply_text("❌ Only group admins can use this command!")
            return
        
        # Check if there are active quizzes
        active_quizzes = [q for q in self.quizzes if q.get('is_active', True)]
        if not active_quizzes:
            await update.message.reply_text("❌ No quizzes available! Please add some quizzes first.")
            return
        
        # NEW: optional subject/folder filters — /rquiz [Subject] [Quiz Folder]
        candidates = None
        if context.args:
            subject_arg = context.args[0]
            all_subjects = self.get_subjects()
            subject_match = next((s for s in all_subjects if s.lower() == subject_arg.lower()), None)
            if not subject_match:
                subject_list = "\n".join(f"• {s}" for s in all_subjects[:20]) or "• (none)"
                await update.message.reply_text(
                    f"❌ Subject '{subject_arg}' not found.\n\n"
                    f"Available subjects:\n{subject_list}\n\n"
                    f"Usage: /rquiz [Subject] [Quiz Folder]"
                )
                return
            folder_match = None
            if len(context.args) > 1:
                folder_arg = ' '.join(context.args[1:])
                folders = self.get_folders(subject_match)
                folder_match = next((f for f in folders if f.lower() == folder_arg.lower()), None)
                if not folder_match:
                    folder_list = "\n".join(f"• {f}" for f in folders[:20]) or "• (none)"
                    await update.message.reply_text(
                        f"❌ Folder '{folder_arg}' not found under {subject_match}.\n\n"
                        f"Available folders:\n{folder_list}"
                    )
                    return
            candidates = [q for q in active_quizzes
                          if q.get('subject', 'General') == subject_match
                          and (folder_match is None or q.get('folder', 'Uncategorized') == folder_match)]
            if not candidates:
                label = f"{subject_match} → {folder_match}" if folder_match else subject_match
                await update.message.reply_text(f"❌ No quizzes available in {label} yet.")
                return
        
        # Ensure group is registered (auto-register if not)
        group = await self.ensure_group_registered(chat_id, chat_title)
        if not group:
            await update.message.reply_text("❌ Failed to register group. Please try again.")
            return
        
        if not group.get('is_active', True):
            # Reactivate the group
            group['is_active'] = True
            self.save_group(group)
        
        # Send typing action
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        
        try:
            # Select random quiz using the same anti-repeat logic
            quiz = self.get_random_quiz(exclude_recent_count=5, candidates=candidates)
            
            if not quiz:
                await update.message.reply_text("❌ Failed to select a quiz. Please try again later.")
                return
            
            # Update quiz stats for manual sends
            quiz['manual_sent_count'] = quiz.get('manual_sent_count', 0) + 1
            quiz['last_sent'] = datetime.now().isoformat()
            self.save_quiz(quiz)
            
            # Track as recently sent
            self.track_recent_quiz(quiz['_id'])
            
            # Update group stats for manual quizzes
            group['manual_quizzes_received'] = group.get('manual_quizzes_received', 0) + 1
            group['last_activity'] = datetime.now().isoformat()
            self.save_group(group)
            
            # Update global stats
            self.stats['manual_quizzes_sent'] = self.stats.get('manual_quizzes_sent', 0) + 1
            self.save_stats()
            
            # Send the quiz (NO confirmation message)
            await self.send_quiz_to_group(group, quiz)
            
            # Only log to console, don't send message to group
            print(f"🎯 Manual quiz sent to {chat_title} by {update.effective_user.first_name}")
            
        except Exception as e:
            print(f"Error sending immediate quiz: {e}")
            await update.message.reply_text("❌ Failed to send quiz. Please try again later.")
    
    # ==========================================================
    # NEW: USER /quiz COMMAND + QUIZ SESSION SYSTEM
    # ==========================================================
    
    async def quiz_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /quiz — browse subjects/folders and play quizzes.
        Works in private chat (any user) AND in groups (group admins / bot admin only)."""
        chat_type = update.effective_chat.type
        
        if chat_type in ['group', 'supergroup']:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            is_admin = self.is_admin(user_id)
            if not is_admin:
                try:
                    chat_member = await context.bot.get_chat_member(chat_id, user_id)
                    if chat_member.status in ['administrator', 'creator']:
                        is_admin = True
                except Exception as e:
                    print(f"Error checking admin status: {e}")
            if not is_admin:
                await update.message.reply_text("❌ Only group admins can use /quiz here!")
                return
        elif chat_type != 'private':
            await update.message.reply_text(
                "❌ /quiz works only in the bot's private chat or in a group (admins only)!"
            )
            return
        
        # Fresh browsing clears any old session (stale Next buttons become no-ops)
        context.user_data['quiz_session'] = None
        
        text, keyboard = self.build_user_subject_menu()
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token):
        """User tapped a subject in /quiz browsing"""
        query = update.callback_query
        subject = self.resolve_subject_token(token)
        if not subject:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        context.user_data['quiz_browse_subject'] = subject
        text, keyboard = self.build_user_folder_menu(subject)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_folder(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token):
        """User tapped a quiz folder in /quiz browsing.
        NEW: if the folder has sub-folders, show the sub-folder picker first."""
        query = update.callback_query
        pair = self.resolve_pair_token(token)
        if not pair:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        subject, folder = pair
        context.user_data['quiz_browse_subject'] = subject
        context.user_data['quiz_browse_folder'] = folder
        subfolders = self.get_subfolders(subject, folder)
        if subfolders:
            text, keyboard = self.build_user_subfolder_menu(subject, folder)
        else:
            text, keyboard = self.build_user_folder_start(subject, folder, '')
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_subfolder(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token):
        """NEW: User picked a specific sub-folder (or 'All Questions in Folder') — show the confirm screen"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        subject, folder, subfolder = triple
        context.user_data['quiz_browse_subject'] = subject
        text, keyboard = self.build_user_folder_start(subject, folder, subfolder)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_pickcount(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token):
        """NEW: User tapped 'Start Quiz' — ask how many questions they want"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        subject, folder, subfolder = triple
        text, keyboard = self.build_quiz_count_menu(subject, folder, subfolder, token)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_count_custom(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token):
        """NEW: User tapped 'Custom Number' for question count — ask them to type it"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        subject, folder, subfolder = triple
        query_filter = {'subject': subject, 'folder': folder, 'is_active': True}
        if subfolder:
            query_filter['subfolder'] = subfolder
        total = self.mongo.count_documents('quizzes', query_filter)
        context.user_data['await'] = 'quiz_custom_count'
        context.user_data['quiz_ctx_token'] = token
        await query.edit_message_text(
            f"✏️ Send the number of questions you want (1-{total}).")
    
    async def handle_qz_count_chosen(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token, count):
        """NEW: User picked a preset question count — ask for the timer next"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        text, keyboard = self.build_quiz_timer_menu(token, count)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_qz_timer_custom(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token, count):
        """NEW: User tapped 'Custom Timer' — ask them to type the number of seconds"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        context.user_data['await'] = 'quiz_custom_timer'
        context.user_data['quiz_ctx_token'] = token
        context.user_data['quiz_setup_count'] = count
        await query.edit_message_text(
            "✏️ Send the time limit per question, in seconds (5-600).")
    
    async def handle_qz_timer_chosen(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token, count, secs):
        """NEW: User picked a preset timer — launch the quiz"""
        query = update.callback_query
        triple = self.resolve_qz_ctx(token)
        if not triple:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        subject, folder, subfolder = triple
        await self.launch_quiz_session(context, query.message.chat_id, subject, folder, subfolder, count, secs, edit_query=query)
    
    async def handle_quiz_custom_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """NEW: Handle free-text custom question-count / custom timer replies (any user)"""
        state = context.user_data.get('await')
        text = (update.message.text or '').strip()
        
        if state == 'quiz_custom_count':
            token = context.user_data.get('quiz_ctx_token')
            triple = self.resolve_qz_ctx(token) if token else None
            if not triple:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            subject, folder, subfolder = triple
            query_filter = {'subject': subject, 'folder': folder, 'is_active': True}
            if subfolder:
                query_filter['subfolder'] = subfolder
            total = self.mongo.count_documents('quizzes', query_filter)
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text(f"❌ Please send a valid positive number (1-{total}).")
                return
            count = min(int(text), total)
            context.user_data['await'] = None
            out_text, keyboard = self.build_quiz_timer_menu(token, count)
            await update.message.reply_text(out_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif state == 'quiz_custom_timer':
            token = context.user_data.get('quiz_ctx_token')
            count = context.user_data.get('quiz_setup_count')
            triple = self.resolve_qz_ctx(token) if token else None
            if not triple or count is None:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                await update.message.reply_text("❌ Please send a valid number of seconds (5-600).")
                return
            secs = int(text)
            context.user_data['await'] = None
            subject, folder, subfolder = triple
            await self.launch_quiz_session(context, update.effective_chat.id, subject, folder, subfolder, count, secs)
        
        # NEW: custom question count for a MULTI-CHAPTER selection
        elif state == 'quiz_multi_custom_count':
            token = context.user_data.get('quiz_multi_ctx_token')
            pair = self.resolve_multi_ctx(token) if token else None
            if not pair:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            subject, folders = pair
            query_filter = {'subject': subject, 'folder': {'$in': list(folders)}, 'is_active': True}
            total = self.mongo.count_documents('quizzes', query_filter)
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text(f"❌ Please send a valid positive number (1-{total}).")
                return
            count = min(int(text), total)
            context.user_data['await'] = None
            out_text, keyboard = self.build_multi_quiz_timer_menu(token, count)
            await update.message.reply_text(out_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
        # NEW: custom timer for a MULTI-CHAPTER selection
        elif state == 'quiz_multi_custom_timer':
            token = context.user_data.get('quiz_multi_ctx_token')
            count = context.user_data.get('quiz_multi_setup_count')
            pair = self.resolve_multi_ctx(token) if token else None
            if not pair or count is None:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                await update.message.reply_text("❌ Please send a valid number of seconds (5-600).")
                return
            secs = int(text)
            context.user_data['await'] = None
            subject, folders = pair
            await self.launch_quiz_session_multi(context, update.effective_chat.id, subject, folders, count, secs)
        
        # NEW: custom question count for a MULTI-SUBJECT selection
        elif state == 'quiz_subjmulti_custom_count':
            token = context.user_data.get('quiz_subjmulti_ctx_token')
            subjects = self.resolve_subj_multi_ctx(token) if token else None
            if not subjects:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            query_filter = {'subject': {'$in': list(subjects)}, 'is_active': True}
            total = self.mongo.count_documents('quizzes', query_filter)
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text(f"❌ Please send a valid positive number (1-{total}).")
                return
            count = min(int(text), total)
            context.user_data['await'] = None
            out_text, keyboard = self.build_subjmulti_timer_menu(token, count)
            await update.message.reply_text(out_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
        # NEW: custom timer for a MULTI-SUBJECT selection
        elif state == 'quiz_subjmulti_custom_timer':
            token = context.user_data.get('quiz_subjmulti_ctx_token')
            count = context.user_data.get('quiz_subjmulti_setup_count')
            subjects = self.resolve_subj_multi_ctx(token) if token else None
            if not subjects or count is None:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                await update.message.reply_text("❌ Please send a valid number of seconds (5-600).")
                return
            secs = int(text)
            context.user_data['await'] = None
            await self.launch_quiz_session_subjmulti(context, update.effective_chat.id, subjects, count, secs)
        
        # NEW: custom question count for the FULL multi-subject × chapter × sub-folder selection
        elif state == 'quiz_subjmulti_full_custom_count':
            token = context.user_data.get('quiz_subjmulti_full_ctx_token')
            combo = self.resolve_subjmulti_full_ctx(token) if token else None
            if not combo:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            subjects, folders, subfolders = combo
            query_filter = {'subject': {'$in': list(subjects)}, 'is_active': True}
            if folders:
                query_filter['folder'] = {'$in': list(folders)}
            if subfolders:
                query_filter['subfolder'] = {'$in': list(subfolders)}
            total = self.mongo.count_documents('quizzes', query_filter)
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text(f"❌ Please send a valid positive number (1-{total}).")
                return
            count = min(int(text), total)
            context.user_data['await'] = None
            out_text, keyboard = self.build_subjmulti_full_timer_menu(token, count)
            await update.message.reply_text(out_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
        # NEW: custom timer for the FULL multi-subject × chapter × sub-folder selection
        elif state == 'quiz_subjmulti_full_custom_timer':
            token = context.user_data.get('quiz_subjmulti_full_ctx_token')
            count = context.user_data.get('quiz_subjmulti_full_setup_count')
            combo = self.resolve_subjmulti_full_ctx(token) if token else None
            if not combo or count is None:
                context.user_data['await'] = None
                await update.message.reply_text("⚠️ Session expired. Please use /quiz to start again.")
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                await update.message.reply_text("❌ Please send a valid number of seconds (5-600).")
                return
            secs = int(text)
            context.user_data['await'] = None
            subjects, folders, subfolders = combo
            await self.launch_quiz_session_subjmulti(
                context, update.effective_chat.id, list(subjects), count, secs,
                folders=list(folders) if folders else None,
                subfolders=list(subfolders) if subfolders else None)
        
        else:
            context.user_data['await'] = None
    
    async def launch_quiz_session(self, context: ContextTypes.DEFAULT_TYPE, chat_id, subject, folder, subfolder, count, secs, edit_query=None):
        """NEW: Create the session (with count limit + timer) and send the first question"""
        session = self.start_quiz_session(context, subject, folder, subfolder, count, secs)
        if not session:
            label = f"{folder} → 📂 {subfolder}" if subfolder else folder
            text = (f"😔 No quiz questions here yet. Please check back later!\n\n"
                    f"📚 Subject: {subject}\n📁 Quiz Folder: {label}")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Folders", callback_data="qz_back_folders")],
                [InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")]
            ])
            if edit_query:
                await edit_query.edit_message_text(text, reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id, text, reply_markup=keyboard)
            return
        
        label = f"{folder} → 📂 {subfolder}" if subfolder else folder
        timer_label = "1 min" if secs == 60 else f"{secs} sec"
        session['chat_id'] = chat_id  # NEW: remember WHERE this quiz is running (group or DM)
        intro = (
            f"▶️ Starting Quiz!\n\n"
            f"📚 {subject}\n📁 {label}\n"
            f"📝 {session['total_questions']} question(s) • ⏱ {timer_label} per question • Random order\n\n"
            f"Answer each poll — the next question comes automatically!\n"
            f"Send /stop anytime to end the quiz and see your score.\n\n"
            f"Good luck! 🍀"
        )
        if edit_query:
            await edit_query.edit_message_text(intro)
        else:
            await context.bot.send_message(chat_id, intro)
        await self.send_session_question(context, chat_id)
    
    async def launch_quiz_session_multi(self, context: ContextTypes.DEFAULT_TYPE, chat_id, subject, folders, count, secs, edit_query=None):
        """NEW: Same as launch_quiz_session, but for a MULTI-CHAPTER selection."""
        session = self.start_quiz_session_multi(context, subject, folders, count, secs)
        label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
        if not session:
            text = (f"😔 No quiz questions here yet. Please check back later!\n\n"
                    f"📚 Subject: {subject}\n📁 Chapters: {label}")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Folders", callback_data="qz_back_folders")],
                [InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")]
            ])
            if edit_query:
                await edit_query.edit_message_text(text, reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id, text, reply_markup=keyboard)
            return
        
        timer_label = "1 min" if secs == 60 else f"{secs} sec"
        session['chat_id'] = chat_id  # NEW: remember WHERE this quiz is running (group or DM)
        intro = (
            f"▶️ Starting Quiz!\n\n"
            f"📚 {subject}\n📁 {label}\n"
            f"📝 {session['total_questions']} question(s) • ⏱ {timer_label} per question • Random order\n\n"
            f"Answer each poll — the next question comes automatically!\n"
            f"Send /stop anytime to end the quiz and see your score.\n\n"
            f"Good luck! 🍀"
        )
        if edit_query:
            await edit_query.edit_message_text(intro)
        else:
            await context.bot.send_message(chat_id, intro)
        await self.send_session_question(context, chat_id)
    
    async def launch_quiz_session_subjmulti(self, context: ContextTypes.DEFAULT_TYPE, chat_id, subjects, count, secs, edit_query=None, folders=None, subfolders=None):
        """NEW: Same as launch_quiz_session, but for a MULTI-SUBJECT selection
        (pools every chapter/folder under each chosen subject).
        folders/subfolders OPTIONALLY narrow it further (also multi-select)."""
        session = self.start_quiz_session_subjmulti(context, subjects, count, secs, folders=folders, subfolders=subfolders)
        label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
        if not session:
            text = (f"😔 No quiz questions here yet. Please check back later!\n\n"
                    f"📚 Subjects: {label}")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")]
            ])
            if edit_query:
                await edit_query.edit_message_text(text, reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id, text, reply_markup=keyboard)
            return
        
        extra_lines = ""
        if folders:
            folder_label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
            extra_lines += f"📁 Chapters: {folder_label}\n"
        if subfolders:
            sf_label = ", ".join(subfolders) if len(subfolders) <= 3 else f"{len(subfolders)} sub-folders selected"
            extra_lines += f"📂 Sub-folders: {sf_label}\n"
        timer_label = "1 min" if secs == 60 else f"{secs} sec"
        session['chat_id'] = chat_id  # NEW: remember WHERE this quiz is running (group or DM)
        intro = (
            f"▶️ Starting Quiz!\n\n"
            f"📚 Subjects: {label}\n"
            f"{extra_lines}"
            f"📝 {session['total_questions']} question(s) • ⏱ {timer_label} per question • Random order\n\n"
            f"Answer each poll — the next question comes automatically!\n"
            f"Send /stop anytime to end the quiz and see your score.\n\n"
            f"Good luck! 🍀"
        )
        if edit_query:
            await edit_query.edit_message_text(intro)
        else:
            await context.bot.send_message(chat_id, intro)
        await self.send_session_question(context, chat_id)
    
    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """NEW: Grade the user's answer, then AUTOMATICALLY send the next question.
        Fires when the user votes in a non-anonymous quiz poll."""
        pa = update.poll_answer
        session = context.user_data.get('quiz_session')
        if not session or session.get('completed'):
            return
        # Only react to the question currently on the user's screen
        if session.get('last_poll_id') != pa.poll_id:
            return
        
        session['answered'] = session.get('answered', 0) + 1
        chosen = pa.option_ids[0] if pa.option_ids else None
        correct_id = session.get('last_correct_option_id')
        is_correct = chosen is not None and chosen == correct_id
        if is_correct:
            session['score'] = session.get('score', 0) + 1
            self.stats['user_quiz_correct'] = self.stats.get('user_quiz_correct', 0) + 1
        self.save_stats()
        
        # NEW: mark this poll as handled so a pending timeout task (if any) skips it
        session['last_poll_id'] = None
        
        # NEW: always reply in the chat the quiz is actually running in (group or DM) —
        # NOT pa.user.id, which would silently reroute a group quiz into the answerer's own DM.
        chat_id = session.get('chat_id', pa.user.id)
        
        # Quick feedback, then the next question follows automatically
        if is_correct:
            feedback = "✅ Correct! 🎉\n\n➡️ Next question..."
        else:
            options = session.get('last_options') or []
            correct_text = options[correct_id] if correct_id is not None and correct_id < len(options) else "?"
            feedback = f"❌ Wrong!\n✅ Correct answer: {correct_text}\n\n➡️ Next question..."
        try:
            await context.bot.send_message(chat_id, feedback)
        except Exception as e:
            print(f"⚠️ Could not send quiz feedback: {e}")
        
        await self.send_session_question(context, chat_id)
    
    def build_session_result(self, session, stopped=False):
        """NEW: Result text with score for a finished/stopped session"""
        total = session.get('total_questions', 0)
        answered = session.get('answered', 0)
        score = session.get('score', 0)
        title = "🛑 Quiz Stopped!" if stopped else "🎉 Quiz Completed!"
        if session.get('is_subj_multi'):
            subjects = session.get('subjects') or []
            subj_label = ", ".join(subjects) if len(subjects) <= 3 else f"{len(subjects)} subjects selected"
            header_lines = f"📚 Subjects: {subj_label}\n"
            folders = session.get('folders') or []
            if folders:
                folder_label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
                header_lines += f"📁 Chapters: {folder_label}\n"
            subfolders = session.get('subfolders') or []
            if subfolders:
                sf_label = ", ".join(subfolders) if len(subfolders) <= 3 else f"{len(subfolders)} sub-folders selected"
                header_lines += f"📂 Sub-folders: {sf_label}\n"
        elif session.get('is_multi'):
            folders = session.get('folders') or []
            folder_label = ", ".join(folders) if len(folders) <= 3 else f"{len(folders)} chapters selected"
            header_lines = f"📚 Subject: {session.get('subject')}\n📁 Chapters: {folder_label}\n"
        else:
            subfolder = session.get('subfolder')
            folder_label = f"{session.get('folder')} → 📂 {subfolder}" if subfolder else session.get('folder')
            header_lines = f"📚 Subject: {session.get('subject')}\n📁 Quiz Folder: {folder_label}\n"
        timer = session.get('timer_seconds', 0)
        timer_label = ("1 min" if timer == 60 else f"{timer} sec") if timer else "No limit"
        text = (
            f"{title}\n\n"
            f"{header_lines}"
            f"⏱ Timer: {timer_label} per question\n\n"
            f"🏆 Your Score: {score}/{answered}\n"
            f"📝 Questions: {answered}/{total} attempted\n"
        )
        if answered:
            text += f"🎯 Accuracy: {score / answered * 100:.0f}%\n"
        text += "\nGreat job! 🎓" if not stopped else "Come back anytime! 💪"
        return text
    
    async def stop_quiz_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """NEW: Handle /stop — end the running quiz session and show the result.
        Works in private chat AND in a group (for whoever started that quiz there)."""
        session = context.user_data.get('quiz_session')
        if not session or session.get('completed'):
            await update.message.reply_text(
                "ℹ️ You have no quiz running.\n"
                "Use /quiz to start a new quiz! 🎮")
            return
        
        session['completed'] = True
        self.save_stats()  # persist score counters
        
        text = self.build_session_result(session, stopped=True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Restart This Quiz", callback_data="qz_restart")],
            [InlineKeyboardButton("📁 Back to Folders", callback_data="qz_back_folders")],
            [InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)
    
    async def handle_qz_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User tapped 'Restart This Quiz' on the completion screen"""
        query = update.callback_query
        session = context.user_data.get('quiz_session')
        is_subj_multi = bool(session and session.get('is_subj_multi'))
        is_multi = bool(session and session.get('is_multi') and not is_subj_multi)
        if is_subj_multi:
            valid = session and session.get('subjects')
        elif is_multi:
            valid = session and session.get('subject') and session.get('folders')
        else:
            valid = session and session.get('subject') and session.get('folder')
        if not valid:
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(
                "⚠️ No quiz session found — browse again below.\n\n" + text,
                reply_markup=InlineKeyboardMarkup(keyboard))
            return
        if is_subj_multi:
            await self.launch_quiz_session_subjmulti(
                context, query.message.chat_id,
                session['subjects'],
                session.get('total_questions'), session.get('timer_seconds', 0),
                edit_query=query,
                folders=session.get('folders'), subfolders=session.get('subfolders'))
        elif is_multi:
            await self.launch_quiz_session_multi(
                context, query.message.chat_id,
                session['subject'], session['folders'],
                session.get('total_questions'), session.get('timer_seconds', 0),
                edit_query=query)
        else:
            await self.launch_quiz_session(
                context, query.message.chat_id,
                session['subject'], session['folder'], session.get('subfolder', ''),
                session.get('total_questions'), session.get('timer_seconds', 0),
                edit_query=query)
    
    async def send_session_question(self, context: ContextTypes.DEFAULT_TYPE, chat_id):
        """Send the next question of the active user quiz session (or completion)"""
        session, quiz = self.get_next_quiz_question(context)
        if not session:
            await context.bot.send_message(
                chat_id, "⚠️ No active quiz session. Use /quiz to start a new quiz!")
            return
        if quiz is None:
            session['completed'] = True
            self.save_stats()  # persist user_quizzes_sent counter
            await self.send_session_complete(context, chat_id, session)
            return
        
        self.stats['user_quizzes_sent'] = self.stats.get('user_quizzes_sent', 0) + 1
        progress = f"Question {session.get('current_question', 0)}/{session.get('total_questions', 0)}"
        base_explanation = self.settings.get('quiz_explanation', "Check back later for results!")
        if session.get('is_subj_multi'):
            subject_disp = f"{len(session.get('subjects') or [])} subjects"
            folders = session.get('folders') or []
            folder_disp = f"{len(folders)} chapters" if folders else "Multiple"
        elif session.get('is_multi'):
            subject_disp = session.get('subject')
            folder_disp = f"{len(session.get('folders') or [])} chapters"
        else:
            subject_disp = session.get('subject')
            folder_disp = session.get('folder')
        explanation = f"📊 {progress} • 📚 {subject_disp} • 📁 {folder_disp}\n\n{base_explanation}"[:200]
        
        # NEW: use the per-question timer chosen at quiz setup (0 = no limit)
        timer_seconds = session.get('timer_seconds', 0) or 0
        
        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"❓ {quiz['question']}",
            options=quiz['options'],
            is_anonymous=False,
            allows_multiple_answers=False,
            type=Poll.QUIZ,
            correct_option_id=quiz['correct_option_id'],
            explanation=explanation,
            open_period=timer_seconds if timer_seconds > 0 else 0,
            protect_content=False
        )
        
        # NEW: remember this poll so the answer handler can grade it and
        # automatically send the next question when the user votes
        session['last_poll_id'] = message.poll.id
        session['last_correct_option_id'] = quiz['correct_option_id']
        session['last_options'] = quiz['options']
        
        # NEW: remember which DB quiz this poll came from, so /qreport can find it precisely
        self.poll_quiz_map[f"{chat_id}:{message.message_id}"] = str(quiz['_id'])
        
        # NEW: if there's a timer, auto-advance when it runs out and the user didn't answer
        if timer_seconds > 0:
            asyncio.create_task(
                self._auto_advance_on_timeout(context, chat_id, message.poll.id, timer_seconds))
    
    async def _auto_advance_on_timeout(self, context: ContextTypes.DEFAULT_TYPE, chat_id, poll_id, delay):
        """NEW: If the user hasn't answered by the time the poll's open_period ends,
        count it as attempted (wrong) and automatically move to the next question."""
        await asyncio.sleep(delay + 2)  # small buffer so Telegram has closed the poll
        session = context.user_data.get('quiz_session')
        if not session or session.get('completed'):
            return
        if session.get('last_poll_id') != poll_id:
            return  # already answered (or already advanced)
        session['answered'] = session.get('answered', 0) + 1
        session['last_poll_id'] = None
        self.save_stats()
        try:
            await context.bot.send_message(chat_id, "⏰ Time's up!\n\n➡️ Next question...")
        except Exception as e:
            print(f"⚠️ Could not send timeout message: {e}")
        await self.send_session_question(context, chat_id)
    
    async def send_session_complete(self, context: ContextTypes.DEFAULT_TYPE, chat_id, session):
        """Send the completion screen (with score) after all questions were answered"""
        self.save_stats()  # persist score counters
        text = self.build_session_result(session, stopped=False)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Restart This Quiz", callback_data="qz_restart")],
            [InlineKeyboardButton("📁 Back to Folders", callback_data="qz_back_folders")],
            [InlineKeyboardButton("📚 Back to Subjects", callback_data="qz_back_subjects")]
        ])
        await context.bot.send_message(
            chat_id, text, reply_markup=keyboard)
    
    # ==========================================================
    # NEW: ADMIN QUIZ-SAVING FLOW (Subject → Folder → Polls → /done)
    # ==========================================================
    
    async def enter_adding_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm quiz-saving mode with the selected subject/folder"""
        add_state = context.user_data.get('add_state') or {}
        subject = add_state.get('subject')
        folder = add_state.get('folder')
        subfolder = add_state.get('subfolder', '')
        folder_label = f"{folder} → 📂 {subfolder}" if subfolder else folder
        
        text = (
            f"✅ Quiz-Saving Mode Activated!\n\n"
            f"Now send Quiz Mode polls. All quizzes you send will be saved under:\n\n"
            f"📚 Subject: {subject}\n"
            f"📁 Quiz Folder: {folder_label}\n\n"
            f"Send as many Quiz Mode polls as you want.\n"
            f"Finish with /done or the button below.\n\n"
            f"💡 Quiz Mode poll: 📎 → Poll → question & options → ✅ Quiz Mode → set correct answer → send"
        )
        keyboard = [[InlineKeyboardButton("✅ Done Adding", callback_data="addquiz_done")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    
    async def finish_adding(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Exit quiz-saving mode and clear the selected subject/folder"""
        add_state = context.user_data.get('add_state') or {}
        subject = add_state.get('subject')
        folder = add_state.get('folder')
        saved = add_state.get('saved_count', 0)
        
        context.user_data['add_state'] = None
        context.user_data['await'] = None
        
        text = (
            f"✅ Finished Adding Quizzes\n\n"
            f"📚 Subject: {subject}\n"
            f"📁 Quiz Folder: {folder}\n"
            f"📝 Saved this session: {saved} quiz(es)\n"
            f"📊 Total quizzes in database: {len(self.quizzes)}\n\n"
            f"You can start again anytime via 📝 Add Quiz."
        )
        keyboard = [
            [InlineKeyboardButton("📝 Add More Quizzes", callback_data="add_quiz")],
            [InlineKeyboardButton("🗂 Manage Quiz Folders", callback_data="manage_folders")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    
    async def done_adding_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /done — exit quiz-saving mode"""
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("This command is for admin only.")
            return
        if update.effective_chat.type != 'private':
            await update.message.reply_text("❌ /done works only in the bot's private chat.")
            return
        add_state = context.user_data.get('add_state')
        if not add_state or not add_state.get('folder'):
            await update.message.reply_text(
                "ℹ️ You are not in quiz-saving mode.\n"
                "Use /start → 📝 Add Quiz to start adding quizzes."
            )
            return
        await self.finish_adding(update, context)
    
    async def handle_awaiting_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin text input for: new subject / new folder / renames"""
        state = context.user_data.get('await')
        name = (update.message.text or '').strip()
        
        if not name:
            await update.message.reply_text("❌ Please send a valid non-empty name.")
            return
        if len(name) > 60:
            name = name[:60]
        
        context.user_data['await'] = None
        
        if state == 'new_subject':
            existing = next((s for s in self.get_subjects() if s.lower() == name.lower()), None)
            subject = existing if existing else name
            context.user_data['add_state'] = {'subject': subject, 'folder': None, 'saved_count': 0}
            text, keyboard = self.build_admin_folder_menu(subject)
            prefix = "📚 Subject already exists — using it:" if existing else "✅ New subject created:"
            await update.message.reply_text(
                f"{prefix} {subject}\n\n{text}",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif state == 'new_folder':
            add_state = context.user_data.get('add_state') or {}
            subject = add_state.get('subject') or context.user_data.get('manage_subject')
            if not subject:
                await update.message.reply_text(
                    "❌ No subject selected. Start again from /start → 📝 Add Quiz.")
                return
            context.user_data['add_state'] = {'subject': subject, 'folder': name, 'saved_count': 0}
            # FIX: show Step 3 (Sub-folder optional) instead of jumping straight
            # into adding mode, so admin can pick/create a sub-folder or skip it.
            text, keyboard = self.build_admin_subfolder_menu(subject, name)
            await update.message.reply_text(
                f"✅ New quiz folder created: {name}\n\n{text}",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif state == 'new_subfolder':
            info = context.user_data.pop('new_subfolder_pair', {}) or {}
            subject = info.get('subject')
            folder = info.get('folder')
            if not subject or not folder:
                await update.message.reply_text(
                    "❌ No quiz folder selected. Start again from 📝 Add Quiz.")
                return
            self.set_admin_selection(context, subject, folder, name)
            await self.enter_adding_mode(update, context)
        
        elif state == 'rename_subject':
            target = context.user_data.pop('rename_target', {}) or {}
            old = target.get('subject')
            if not old:
                await update.message.reply_text(
                    "❌ Rename target lost. Please try again from 🗂 Manage Quiz Folders.")
                return
            result = self.mongo.update_many('quizzes', {'subject': old}, {'$set': {'subject': name}})
            modified = result.modified_count if result else 0
            self.quizzes = self.load_quizzes()
            await update.message.reply_text(
                f"✅ Subject Renamed\n\n📚 {old} → {name}\n📝 {modified} quiz question(s) updated",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗂 Manage Quiz Folders", callback_data="manage_folders")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
                ]))
        
        elif state == 'rename_folder':
            target = context.user_data.pop('rename_target', {}) or {}
            subject = target.get('subject')
            old = target.get('folder')
            if not subject or not old:
                await update.message.reply_text(
                    "❌ Rename target lost. Please try again from 🗂 Manage Quiz Folders.")
                return
            result = self.mongo.update_many(
                'quizzes', {'subject': subject, 'folder': old}, {'$set': {'folder': name}})
            modified = result.modified_count if result else 0
            self.quizzes = self.load_quizzes()
            await update.message.reply_text(
                f"✅ Quiz Folder Renamed\n\n📚 {subject}\n📁 {old} → {name}\n📝 {modified} quiz question(s) updated",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗂 Manage Quiz Folders", callback_data="manage_folders")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
                ]))
        
        else:
            await update.message.reply_text(
                "❌ Unknown input state. Please use /start and try again.")
    
    # ==========================================================
    # NEW: 🗂 MANAGE QUIZ FOLDERS DASHBOARD
    # ==========================================================
    
    async def show_manage_folders(self, update: Update, context: ContextTypes.DEFAULT_TYPE, notice=None):
        """Manage Quiz Folders home screen"""
        query = update.callback_query
        structure = self.get_structure()
        total_subjects = len(structure['subjects'])
        total_folders = sum(len(folders) for folders in structure['folders'].values())
        total_quizzes = sum(structure['subjects'].values())
        
        text = (
            f"🗂 Quiz Folder Management\n\n"
            f"📚 Subjects: {total_subjects}\n"
            f"📁 Quiz Folders: {total_folders}\n"
            f"📝 Quiz Questions: {total_quizzes}\n\n"
            f"Quizzes are organized as: Subject → Quiz Folder → Sub-folder (optional) → Questions"
        )
        if notice:
            text = notice + "\n\n" + text
        keyboard = [
            [InlineKeyboardButton("📚 View Subjects", callback_data="mf_subjview")],
            [InlineKeyboardButton("➕ Create Subject", callback_data="mf_newsubj")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_manage_subjects(self, update: Update, context: ContextTypes.DEFAULT_TYPE, notice=None):
        """List all subjects with quiz and folder counts"""
        query = update.callback_query
        structure = self.get_structure()
        subjects = structure['subjects']
        
        text = "📚 Subjects\n\nTap a subject to view its quiz folders:"
        if not subjects:
            text = ("📚 Subjects\n\n😔 No subjects yet.\n"
                    "Create one below, then send Quiz Mode polls to fill it.")
        if notice:
            text = notice + "\n\n" + text
        
        keyboard = []
        for name in sorted(subjects.keys()):
            folder_count = len(structure['folders'].get(name, {}))
            token = self.register_subject_token(name)
            keyboard.append([InlineKeyboardButton(
                f"📚 {name} — {subjects[name]} quizzes, {folder_count} folders",
                callback_data=f"mf_subj_{token}")])
        keyboard.append([InlineKeyboardButton("➕ Create Subject", callback_data="mf_newsubj")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="mf_home")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_manage_subject_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE, subject):
        """Show folders inside a subject (manage view)"""
        query = update.callback_query
        context.user_data['manage_subject'] = subject
        structure = self.get_structure()
        folders = structure['folders'].get(subject, {})
        subject_token = self.register_subject_token(subject)
        
        text = (
            f"📚 Subject: {subject}\n"
            f"📝 Total questions: {structure['subjects'].get(subject, 0)}\n"
            f"📁 Folders ({len(folders)}):"
        )
        if not folders:
            text += "\n\n😔 No folders in this subject yet."
        
        keyboard = []
        for folder_name in sorted(folders.keys()):
            token = self.register_pair_token(subject, folder_name)
            keyboard.append([InlineKeyboardButton(
                f"📁 {folder_name} ({folders[folder_name]})",
                callback_data=f"mf_fold_{token}")])
        keyboard.append([InlineKeyboardButton("➕ Create Quiz Folder", callback_data="mf_newfold")])
        keyboard.append([InlineKeyboardButton("✏️ Rename Subject", callback_data=f"mf_rensub_{subject_token}")])
        keyboard.append([InlineKeyboardButton("🗑️ Delete Subject", callback_data=f"mf_delsub_{subject_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Subjects", callback_data="mf_subjview")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_manage_folder_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE, subject, folder):
        """Show details and actions for one quiz folder (incl. its sub-folders)"""
        query = update.callback_query
        count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder})
        root_count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'subfolder': ''})
        subfolders = self.get_subfolders(subject, folder)
        pair_token = self.register_pair_token(subject, folder)
        subject_token = self.register_subject_token(subject)
        
        text = (
            f"📁 Quiz Folder Details\n\n"
            f"📚 Subject: {subject}\n"
            f"📁 Folder: {folder}\n"
            f"📝 Questions inside (total): {count}\n"
            f"📄 Directly in folder (no sub-folder): {root_count}\n"
            f"📂 Sub-folders: {len(subfolders)}"
        )
        keyboard = []
        for name in subfolders:
            cnt = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'subfolder': name})
            token = self.register_qz_ctx(subject, folder, name)
            keyboard.append([InlineKeyboardButton(f"📂 {name} ({cnt})", callback_data=f"mf_subf_{token}")])
        keyboard.append([InlineKeyboardButton("➕ Create Sub-folder", callback_data=f"mf_newsubf_{pair_token}")])
        keyboard.append([InlineKeyboardButton("➕ Add Quizzes Here", callback_data=f"mf_addhere_{pair_token}")])
        keyboard.append([InlineKeyboardButton("✏️ Rename Quiz Folder", callback_data=f"mf_renfold_{pair_token}")])
        keyboard.append([InlineKeyboardButton("🗑️ Delete Quiz Folder", callback_data=f"mf_delfold_{pair_token}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"mf_subj_{subject_token}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_manage_subfolder_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE, subject, folder, subfolder):
        """NEW: Show details and actions for one sub-folder"""
        query = update.callback_query
        count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'subfolder': subfolder})
        ctx_token = self.register_qz_ctx(subject, folder, subfolder)
        pair_token = self.register_pair_token(subject, folder)
        
        text = (
            f"📂 Sub-folder Details\n\n"
            f"📚 Subject: {subject}\n"
            f"📁 Quiz Folder: {folder}\n"
            f"📂 Sub-folder: {subfolder}\n"
            f"📝 Questions inside: {count}"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Add Quizzes Here", callback_data=f"mf_addheresubf_{ctx_token}")],
            [InlineKeyboardButton("🗑️ Delete Sub-folder", callback_data=f"mf_delsubf_{ctx_token}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"mf_fold_{pair_token}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def report_quiz_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /qreport command - report a quiz for review.
        Works in groups AND in the bot's private chat (e.g. while playing a /quiz session)."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        message_id = update.effective_message.message_id
        is_private = update.effective_chat.type == 'private'
        
        # Check if the message is a reply to a quiz
        if not update.message.reply_to_message or not update.message.reply_to_message.poll:
            await update.message.reply_text(
                "❌ Please reply to a quiz message with /qreport!\n\n"
                "**Usage:**\n"
                "1. Find a quiz poll sent by the bot\n"
                "2. Reply to that quiz message\n"
                "3. Send `/qreport`\n\n"
                "The bot will forward the quiz to the admin for review."
            )
            return
        
        replied_poll = update.message.reply_to_message.poll
        
        # Check if it's a quiz mode poll (has correct_option_id)
        if replied_poll.correct_option_id is None:
            await update.message.reply_text("❌ This is not a quiz! Only quiz polls can be reported.")
            return
        
        replied_message_id = update.message.reply_to_message.message_id
        
        # NEW: try to trace this poll back to its exact DB document (works for both
        # group broadcasts and private /quiz sessions — falls back gracefully if not found,
        # e.g. after a bot restart)
        quiz_id = self.poll_quiz_map.get(f"{chat_id}:{replied_message_id}")
        
        # Extract quiz information
        quiz_info = {
            'chat_id': chat_id,
            'message_id': replied_message_id,
            'question': replied_poll.question,
            'options': [option.text for option in replied_poll.options],
            'correct_option_id': replied_poll.correct_option_id,
            'reported_by': {
                'user_id': user_id,
                'username': update.effective_user.username,
                'first_name': update.effective_user.first_name,
            },
            'report_time': datetime.now().isoformat(),
            'group_name': "🔒 Private Chat (DM)" if is_private else update.effective_chat.title,
            # NEW: message links only make sense for groups; DM messages aren't linkable
            'original_message_link': (
                None if is_private else f"https://t.me/c/{str(chat_id)[4:]}/{replied_message_id}"),
            'quiz_id': quiz_id,   # NEW: precise DB link for admin edit/delete, may be None
        }
        
        # Generate a unique report ID
        report_id = f"report_{chat_id}_{message_id}"
        
        # Save report to MongoDB
        self.mongo.insert_one('quiz_reports', {
            '_id': report_id,
            'status': 'pending',  # pending, reviewed, deleted, ignored
            **quiz_info
        })
        
        # Update stats
        self.stats['quiz_reports_received'] = self.stats.get('quiz_reports_received', 0) + 1
        self.save_stats()
        
        # Send confirmation to the user (with self-destruct notice, groups only —
        # keep the confirmation visible in DM so it doesn't feel like it vanished)
        try:
            confirmation_msg = await update.message.reply_text(
                f"✅ **Quiz Reported Successfully!**\n\n"
                f"📝 **Question:** {replied_poll.question[:100]}...\n\n"
                f"The quiz has been forwarded to the admin for review.\n"
                f"Thank you for helping improve the quiz quality!" +
                ("\n\n⏰ _This confirmation will self-destruct in 10 seconds..._" if not is_private else "")
            )
            
            if not is_private:
                # Delete the confirmation after 10 seconds to avoid message clutter
                asyncio.create_task(self.delete_message_after_delay(chat_id, confirmation_msg.message_id, 10))
        except Exception as e:
            print(f"⚠️ Could not send confirmation message (might be deleted): {e}")
            # Continue anyway - the report is already saved
        
        # Send the report to admin (this is the critical part)
        try:
            await self.send_quiz_report_to_admin(context, quiz_info, report_id)
        except Exception as e:
            print(f"❌ Error sending report to admin: {e}")
            # Log the error but don't show to user to avoid confusion
    
    async def delete_message_after_delay(self, chat_id: int, message_id: int, delay_seconds: int):
        """Delete a message after a delay"""
        try:
            await asyncio.sleep(delay_seconds)
            await self.application.bot.delete_message(chat_id=chat_id, message_id=message_id)
            print(f"🗑️ Auto-deleted message {message_id} in chat {chat_id}")
        except Exception as e:
            # Message might have already been deleted by group settings or bot doesn't have permission
            print(f"⚠️ Could not delete message {message_id} in chat {chat_id}: {e}")
    
    async def view_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /view command - view a specific report by ID"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a report ID.\n\n"
                "**Usage:** `/view <report_id>`\n\n"
                "**Example:** `/view report_123456789_123`\n\n"
                "You can find report IDs in the reports dashboard."
            )
            return
        
        report_id = context.args[0]
        
        # Find the report
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        
        if not report:
            await update.message.reply_text(
                f"❌ Report not found: `{report_id}`\n\n"
                f"Make sure you entered the correct report ID."
            )
            return
        
        # Display the report with action buttons
        await self.display_report(update, context, report)
    
    async def display_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report):
        """Display a report with action buttons"""
        # Format quiz information
        options_text = "\n".join([f"• {option}" for option in report['options']])
        correct_answer = report['options'][report['correct_option_id']]
        
        # Handle username display
        username = report['reported_by']['username']
        username_display = f" (@{username})" if username else ""
        
        # Format status
        status_emoji = "🟡" if report.get('status') == 'pending' else "🟢" if report.get('status') == 'ignored' else "🔴"
        status_text = {
            'pending': 'Pending',
            'ignored': 'Ignored',
            'deleted': 'Deleted'
        }.get(report.get('status'), 'Unknown')
        
        # Use HTML formatting to avoid Markdown parsing errors
        link_line = (f"• 🔗 Message: <a href='{report['original_message_link']}'>View Original</a>\n"
                     if report.get('original_message_link') else "")
        report_text = (
            f"📋 <b>Report Details</b>\n\n"
            f"📝 <b>Question:</b> {report['question']}\n\n"
            f"📋 <b>Options:</b>\n{options_text}\n\n"
            f"✅ <b>Correct Answer:</b> {correct_answer}\n\n"
            f"📊 <b>Report Information:</b>\n"
            f"• 👤 Reported by: {report['reported_by']['first_name']}{username_display}\n"
            f"• 👥 Group: {report['group_name']}\n"
            f"• 🕐 Time: {datetime.fromisoformat(report['report_time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 📊 Status: {status_emoji} {status_text}\n"
            f"{link_line}"
            f"• 🆔 Report ID: <code>{report['_id']}</code>\n\n"
        )
        
        # Add action taken info if available
        if report.get('action_taken'):
            action_time = datetime.fromisoformat(report.get('action_time', report['report_time'])).strftime('%Y-%m-%d %H:%M:%S')
            report_text += f"⚡ <b>Action Taken:</b> {report.get('action_taken', 'None')} at {action_time}\n\n"
        
        report_text += "<b>What would you like to do with this quiz?</b>"
        
        # Create action buttons based on status
        if report.get('status') == 'pending':
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Edit / Replace Quiz", callback_data=f"edit_quiz_{report['_id']}")
                ],
                [
                    InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"delete_quiz_{report['_id']}"),
                    InlineKeyboardButton("👁️ Ignore Report", callback_data=f"ignore_report_{report['_id']}")
                ],
                [
                    InlineKeyboardButton("📝 View Similar Quizzes", callback_data=f"view_similar_{report['_id']}"),
                    InlineKeyboardButton("📊 View All Reports", callback_data="view_reports")
                ],
                [InlineKeyboardButton("🔙 Back to Reports", callback_data="view_reports")]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton("📊 View All Reports", callback_data="view_reports"),
                    InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")
                ]
            ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                report_text, 
                reply_markup=reply_markup, 
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                report_text, 
                reply_markup=reply_markup, 
                parse_mode='HTML'
            )
    
    async def send_quiz_report_to_admin(self, context: ContextTypes.DEFAULT_TYPE, quiz_info: dict, report_id: str):
        """Send quiz report to admin with action buttons"""
        
        # Format quiz information
        options_text = "\n".join([f"• {option}" for option in quiz_info['options']])
        correct_answer = quiz_info['options'][quiz_info['correct_option_id']]
        
        # Handle username display
        username = quiz_info['reported_by']['username']
        username_display = f" (@{username})" if username else ""
        
        # Use HTML formatting instead of Markdown to avoid parsing errors
        link_line = (f"• 🔗 Message: <a href='{quiz_info['original_message_link']}'>View Original</a>\n"
                     if quiz_info.get('original_message_link') else "")
        report_text = (
            f"⚠️ <b>QUIZ REPORTED FOR REVIEW</b>\n\n"
            f"📝 <b>Question:</b> {quiz_info['question']}\n\n"
            f"📋 <b>Options:</b>\n{options_text}\n\n"
            f"✅ <b>Correct Answer:</b> {correct_answer}\n\n"
            f"📊 <b>Report Details:</b>\n"
            f"• 👤 Reported by: {quiz_info['reported_by']['first_name']}{username_display}\n"
            f"• 👥 Group: {quiz_info['group_name']}\n"
            f"• 🕐 Time: {datetime.fromisoformat(quiz_info['report_time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{link_line}"
            f"• 🆔 Report ID: <code>{report_id}</code>\n\n"
            f"<b>What would you like to do with this quiz?</b>"
        )
        
        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("✏️ Edit / Replace Quiz", callback_data=f"edit_quiz_{report_id}")
            ],
            [
                InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"delete_quiz_{report_id}"),
                InlineKeyboardButton("👁️ Ignore Report", callback_data=f"ignore_report_{report_id}")
            ],
            [
                InlineKeyboardButton("📝 View Similar Quizzes", callback_data=f"view_similar_{report_id}"),
                InlineKeyboardButton("📊 View All Reports", callback_data="view_reports")
            ],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="start_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send to admin with HTML parse mode
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=report_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            print(f"✅ Report sent to admin: {report_id}")
        except Exception as e:
            print(f"❌ Error sending report to admin: {e}")
            # Try sending a simplified version without HTML
            try:
                simple_text = (
                    f"⚠️ QUIZ REPORTED FOR REVIEW\n\n"
                    f"Question: {quiz_info['question'][:200]}...\n\n"
                    f"Reported by: {quiz_info['reported_by']['first_name']} in {quiz_info['group_name']}\n"
                    f"Time: {datetime.fromisoformat(quiz_info['report_time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Report ID: {report_id}\n\n"
                    f"Use /view {report_id} to view full details"
                )
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=simple_text,
                    reply_markup=reply_markup
                )
            except Exception as e2:
                print(f"❌ Failed to send even simple report: {e2}")
                # Last resort: log to console
                print(f"📋 QUIZ REPORT (Not sent to admin): {report_id}")
                print(f"Question: {quiz_info['question']}")
                print(f"Reported by: {quiz_info['reported_by']['first_name']}")
                print(f"Group: {quiz_info['group_name']}")
    
    async def handle_delete_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """Handle delete quiz action from admin"""
        query = update.callback_query
        await query.answer()
        
        # Get report details
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        if not report:
            await query.edit_message_text("❌ Report not found or already processed.")
            return
        
        # Find and delete the quiz from database
        deleted_count = 0
        similar_quizzes = []
        
        # Find quizzes with similar question (case-insensitive partial match)
        all_quizzes = self.mongo.find('quizzes', {})
        for quiz in all_quizzes:
            if quiz['question'].lower() == report['question'].lower():
                # Exact match - delete
                self.mongo.delete_one('quizzes', {'_id': quiz['_id']})
                deleted_count += 1
            elif report['question'].lower() in quiz['question'].lower() or quiz['question'].lower() in report['question'].lower():
                # Partial match - add to similar list
                similar_quizzes.append(quiz)
        
        # Update report status
        self.mongo.update_one('quiz_reports', {'_id': report_id}, {
            '$set': {
                'status': 'deleted',
                'action_taken': 'quiz_deleted',
                'deleted_quizzes': deleted_count,
                'action_time': datetime.now().isoformat()
            }
        })
        
        # Update stats
        self.stats['quizzes_deleted_by_reports'] = self.stats.get('quizzes_deleted_by_reports', 0) + deleted_count
        self.save_stats()
        
        # Reload quizzes
        self.quizzes = self.load_quizzes()
        
        # Prepare response
        response_text = (
            f"✅ **Quiz Deleted Successfully!**\n\n"
            f"🗑️ Deleted {deleted_count} quiz(es) with matching question:\n"
            f"`{report['question'][:100]}...`\n\n"
        )
        
        if similar_quizzes:
            response_text += f"⚠️ Found {len(similar_quizzes)} similar quizzes:\n"
            for i, quiz in enumerate(similar_quizzes[:5], 1):  # Show only first 5
                response_text += f"{i}. {quiz['question'][:80]}...\n"
            
            if len(similar_quizzes) > 5:
                response_text += f"... and {len(similar_quizzes) - 5} more\n"
            
            # Add option to delete all similar
            keyboard = [
                [InlineKeyboardButton("🗑️ Delete All Similar", callback_data=f"delete_similar_{report_id}")],
                [InlineKeyboardButton("✅ Done", callback_data="close_report")]
            ]
        else:
            keyboard = [[InlineKeyboardButton("✅ Done", callback_data="close_report")]]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(response_text, reply_markup=reply_markup)
    
    async def handle_delete_similar_quizzes(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """Delete all similar quizzes"""
        query = update.callback_query
        await query.answer()
        
        # Get report details
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        if not report:
            await query.edit_message_text("❌ Report not found.")
            return
        
        # Find and delete all similar quizzes
        deleted_count = 0
        all_quizzes = self.mongo.find('quizzes', {})
        
        for quiz in all_quizzes:
            # Check for similarity (partial match in either direction)
            if (report['question'].lower() in quiz['question'].lower() or 
                quiz['question'].lower() in report['question'].lower()):
                self.mongo.delete_one('quizzes', {'_id': quiz['_id']})
                deleted_count += 1
        
        # Update report
        self.mongo.update_one('quiz_reports', {'_id': report_id}, {
            '$set': {
                'additional_deleted': deleted_count,
                'total_deleted': report.get('deleted_quizzes', 0) + deleted_count,
                'action_time': datetime.now().isoformat()
            }
        })
        
        # Update stats
        self.stats['quizzes_deleted_by_reports'] = self.stats.get('quizzes_deleted_by_reports', 0) + deleted_count
        self.save_stats()
        
        # Reload quizzes
        self.quizzes = self.load_quizzes()
        
        response_text = (
            f"✅ **All Similar Quizzes Deleted!**\n\n"
            f"🗑️ Deleted {deleted_count} similar quizzes\n"
            f"📝 Total deleted for this report: {report.get('deleted_quizzes', 0) + deleted_count}\n\n"
            f"The quiz database has been cleaned."
        )
        
        keyboard = [[InlineKeyboardButton("✅ Done", callback_data="close_report")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(response_text, reply_markup=reply_markup)
    
    async def handle_ignore_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """Handle ignore report action"""
        query = update.callback_query
        await query.answer()
        
        # Update report status
        self.mongo.update_one('quiz_reports', {'_id': report_id}, {
            '$set': {
                'status': 'ignored',
                'action_taken': 'ignored',
                'action_time': datetime.now().isoformat()
            }
        })
        
        await query.edit_message_text(
            "✅ **Report Ignored**\n\n"
            "The quiz report has been marked as ignored.\n"
            "No action was taken on the quiz.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Close", callback_data="close_report")]])
        )
    
    async def handle_view_similar(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """View similar quizzes in database"""
        query = update.callback_query
        await query.answer()
        
        # Get report details
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        if not report:
            await query.edit_message_text("❌ Report not found.")
            return
        
        # Find similar quizzes
        similar_quizzes = []
        for quiz in self.quizzes:
            # Check for similarity
            if (report['question'].lower() in quiz['question'].lower() or 
                quiz['question'].lower() in report['question'].lower()):
                similar_quizzes.append(quiz)
        
        if not similar_quizzes:
            response_text = (
                f"📝 **No Similar Quizzes Found**\n\n"
                f"The reported question:\n`{report['question']}`\n\n"
                f"Was not found in the database.\n"
                f"It might have been already deleted or never saved."
            )
            
            keyboard = [
                [InlineKeyboardButton("🔙 Back to Report", callback_data=f"report_back_{report_id}")],
                [InlineKeyboardButton("✅ Close", callback_data="close_report")]
            ]
        else:
            response_text = f"📝 **Found {len(similar_quizzes)} Similar Quiz(es)**\n\n"
            
            for i, quiz in enumerate(similar_quizzes[:10], 1):  # Show only first 10
                status = "✅ Active" if quiz.get('is_active', True) else "❌ Inactive"
                sent_count = quiz.get('sent_count', 0)
                manual_count = quiz.get('manual_sent_count', 0)
                
                response_text += (
                    f"**{i}. {quiz['question'][:80]}...**\n"
                    f"   Status: {status} | Auto: {sent_count} | Manual: {manual_count}\n"
                    f"   ID: `{quiz['_id']}`\n\n"
                )
            
            if len(similar_quizzes) > 10:
                response_text += f"... and {len(similar_quizzes) - 10} more similar quizzes\n\n"
            
            response_text += "**Options:**"
            
            keyboard = [
                [
                    InlineKeyboardButton("🗑️ Delete All", callback_data=f"delete_similar_{report_id}"),
                    InlineKeyboardButton("🗑️ Delete Only Exact", callback_data=f"delete_quiz_{report_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back to Report", callback_data=f"report_back_{report_id}"),
                    InlineKeyboardButton("✅ Close", callback_data="close_report")
                ]
            ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(response_text, reply_markup=reply_markup)
    
    async def handle_edit_quiz_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """NEW: Entry point for '✏️ Edit / Replace Quiz' on a report — locates the exact
        DB quiz doc (via the report's saved quiz_id, or falls back to an exact question
        match, same rule the Delete flow uses) and shows edit options."""
        query = update.callback_query
        await query.answer()
        
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        if not report:
            await query.edit_message_text("❌ Report not found or already processed.")
            return
        
        candidates = []
        quiz_id = report.get('quiz_id')
        if quiz_id:
            quiz = self.get_quiz_by_id(quiz_id)
            if quiz:
                candidates = [quiz]
        if not candidates:
            # Fallback: exact question match (case-insensitive) — same rule Delete uses
            all_quizzes = self.mongo.find('quizzes', {})
            candidates = [q for q in all_quizzes if q['question'].lower() == report['question'].lower()]
        
        if not candidates:
            await query.edit_message_text(
                "❌ Couldn't locate the original quiz in the database.\n"
                "It may have already been deleted or edited.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Report", callback_data=f"report_back_{report_id}")]
                ]))
            return
        
        if len(candidates) == 1:
            await self.show_edit_quiz_options(query, candidates[0], report_id)
            return
        
        # Multiple exact-question matches — let the admin pick which one to edit
        text = f"⚠️ Found {len(candidates)} quizzes with this exact question. Which one do you want to edit?\n\n"
        keyboard = []
        for i, quiz in enumerate(candidates[:10], 1):
            token = self.register_edit_ctx(str(quiz['_id']), report_id)
            folder_bit = f" → {quiz.get('folder')}" if quiz.get('folder') else ""
            label = f"{i}. {quiz.get('subject', '?')}{folder_bit}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"editq_pick_{token}")])
        if len(candidates) > 10:
            text += f"(Showing first 10 of {len(candidates)})\n\n"
        keyboard.append([InlineKeyboardButton("🔙 Back to Report", callback_data=f"report_back_{report_id}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_edit_quiz_options(self, query, quiz, report_id):
        """NEW: Show the actual edit choices for one specific quiz doc."""
        token = self.register_edit_ctx(str(quiz['_id']), report_id)
        options_text = "\n".join([f"• {o}" for o in quiz['options']])
        correct = quiz['options'][quiz['correct_option_id']]
        location = f"📚 {quiz.get('subject', '?')} → 📁 {quiz.get('folder', '?')}"
        if quiz.get('subfolder'):
            location += f" → 📂 {quiz['subfolder']}"
        text = (
            f"✏️ <b>Edit Quiz</b>\n\n"
            f"{location}\n\n"
            f"📝 <b>Question:</b> {quiz['question']}\n\n"
            f"📋 <b>Options:</b>\n{options_text}\n\n"
            f"✅ <b>Correct:</b> {correct}\n\n"
            f"Choose what you want to change:"
        )
        keyboard = [
            [InlineKeyboardButton("📝 Edit Question Text Only", callback_data=f"editq_text_{token}")],
            [InlineKeyboardButton("🔁 Replace Entire Quiz (send new poll)", callback_data=f"editq_poll_{token}")],
            [InlineKeyboardButton("🔙 Back to Report", callback_data=f"report_back_{report_id}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    async def handle_editq_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
        """NEW: Admin picked one specific quiz from the multi-match list."""
        query = update.callback_query
        await query.answer()
        pair = self.resolve_edit_ctx(token)
        if not pair:
            await query.edit_message_text("⚠️ This session expired — please open the report again.")
            return
        quiz_id, report_id = pair
        quiz = self.get_quiz_by_id(quiz_id)
        if not quiz:
            await query.edit_message_text("❌ Quiz not found (may have been deleted).")
            return
        await self.show_edit_quiz_options(query, quiz, report_id)
    
    async def handle_editq_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
        """NEW: Admin chose to edit ONLY the question text — ask for the new text."""
        query = update.callback_query
        await query.answer()
        pair = self.resolve_edit_ctx(token)
        if not pair:
            await query.edit_message_text("⚠️ This session expired — please open the report again.")
            return
        quiz_id, report_id = pair
        context.user_data['await'] = 'edit_quiz_question'
        context.user_data['edit_quiz_ctx'] = {'quiz_id': quiz_id, 'report_id': report_id}
        await query.edit_message_text(
            "✏️ Send the NEW question text now.\n\n"
            "(Options and the correct answer stay the same. If you need to change those too, "
            "use 🔁 Replace Entire Quiz instead.)"
        )
    
    async def handle_editq_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
        """NEW: Admin chose to replace the entire quiz — ask for a new Quiz Mode poll."""
        query = update.callback_query
        await query.answer()
        pair = self.resolve_edit_ctx(token)
        if not pair:
            await query.edit_message_text("⚠️ This session expired — please open the report again.")
            return
        quiz_id, report_id = pair
        context.user_data['quiz_edit_replace_pending'] = {'quiz_id': quiz_id, 'report_id': report_id}
        await query.edit_message_text(
            "🔁 Send the NEW Quiz Mode poll now — it will completely replace the question, "
            "options, and correct answer of the reported quiz.\n"
            "(Subject/folder location stays the same.)\n\n"
            "💡 How to create a Quiz Mode poll:\n"
            "1. Tap the 📎 attachment icon → Poll\n"
            "2. Enter your question and options\n"
            "3. ✅ Enable 'Quiz Mode' and set the correct answer\n"
            "4. Send it to me"
        )
    
    async def handle_edit_quiz_question_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """NEW: Handle the admin's text reply for 'Edit Question Text Only'."""
        ctx = context.user_data.get('edit_quiz_ctx') or {}
        quiz_id = ctx.get('quiz_id')
        report_id = ctx.get('report_id')
        context.user_data['await'] = None
        context.user_data['edit_quiz_ctx'] = None
        
        new_question = (update.message.text or '').strip()
        if not new_question:
            await update.message.reply_text("❌ Please send a valid, non-empty question.")
            return
        if not quiz_id:
            await update.message.reply_text("⚠️ Edit session expired. Please open the report again.")
            return
        
        quiz = self.get_quiz_by_id(quiz_id)
        if not quiz:
            await update.message.reply_text("❌ Quiz not found (may have been deleted). Nothing was changed.")
            return
        
        self.mongo.update_one('quizzes', {'_id': quiz['_id']}, {'$set': {'question': new_question}})
        self.quizzes = self.load_quizzes()
        
        if report_id:
            self.mongo.update_one('quiz_reports', {'_id': report_id}, {'$set': {
                'status': 'reviewed',
                'action_taken': 'question_edited',
                'action_time': datetime.now().isoformat()
            }})
        
        await update.message.reply_text(
            f"✅ **Question Updated!**\n\n"
            f"📝 New question: {new_question}\n\n"
            f"The quiz has been updated in place — everything else is unchanged.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="close_report")]])
        )
    
    async def handle_edit_quiz_replace_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE, poll):
        """NEW: Handle the admin's new Quiz Mode poll for 'Replace Entire Quiz'."""
        pending = context.user_data.get('quiz_edit_replace_pending') or {}
        quiz_id = pending.get('quiz_id')
        report_id = pending.get('report_id')
        context.user_data['quiz_edit_replace_pending'] = None
        
        if poll.correct_option_id is None:
            await update.message.reply_text(
                "❌ This is a regular poll, not a quiz!\n\n"
                "I need a QUIZ MODE poll (with a correct answer set) to replace it.\n"
                "Please create a new poll with Quiz Mode enabled and send it again."
            )
            context.user_data['quiz_edit_replace_pending'] = pending  # let the admin retry
            return
        
        if not quiz_id:
            await update.message.reply_text("⚠️ Edit session expired. Please open the report again.")
            return
        
        quiz = self.get_quiz_by_id(quiz_id)
        if not quiz:
            await update.message.reply_text("❌ Quiz not found (may have been deleted). Nothing was changed.")
            return
        
        self.mongo.update_one('quizzes', {'_id': quiz['_id']}, {'$set': {
            'question': poll.question,
            'options': [option.text for option in poll.options],
            'correct_option_id': poll.correct_option_id,
        }})
        self.quizzes = self.load_quizzes()
        
        if report_id:
            self.mongo.update_one('quiz_reports', {'_id': report_id}, {'$set': {
                'status': 'reviewed',
                'action_taken': 'quiz_replaced',
                'action_time': datetime.now().isoformat()
            }})
        
        correct_answer = poll.options[poll.correct_option_id].text
        location = f"📚 {quiz.get('subject', '?')} → 📁 {quiz.get('folder', '?')}"
        if quiz.get('subfolder'):
            location += f" → 📂 {quiz['subfolder']}"
        await update.message.reply_text(
            f"✅ **Quiz Replaced!**\n\n"
            f"{location}\n\n"
            f"📝 New Question: {poll.question}\n"
            f"✅ New Correct Answer: {correct_answer}\n\n"
            f"The old question/options/answer have been replaced in place.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="close_report")]])
        )
    
    async def handle_view_reports(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View all pending quiz reports"""
        query = update.callback_query
        await query.answer()
        
        # Get all pending reports
        pending_reports = self.mongo.find('quiz_reports', {'status': 'pending'})
        total_reports = self.mongo.find('quiz_reports', {})
        
        if not pending_reports:
            response_text = (
                f"📊 **Quiz Reports Dashboard**\n\n"
                f"✅ No pending reports!\n\n"
                f"📈 **Statistics:**\n"
                f"• Total reports: {len(total_reports)}\n"
                f"• Pending: 0\n"
                f"• Resolved: {len([r for r in total_reports if r['status'] != 'pending'])}\n"
            )
            
            keyboard = [[InlineKeyboardButton("✅ Close", callback_data="close_report")]]
        else:
            response_text = (
                f"📊 **Quiz Reports Dashboard**\n\n"
                f"⚠️ **Pending Reports: {len(pending_reports)}**\n\n"
            )
            
            # Create buttons for each report
            keyboard = []
            for i, report in enumerate(pending_reports[:5], 1):  # Show only first 5
                report_time = datetime.fromisoformat(report['report_time']).strftime('%m/%d %H:%M')
                link_bit = (f"[View Original]({report['original_message_link']})\n"
                            if report.get('original_message_link') else "🔒 Private chat report\n")
                response_text += (
                    f"{i}. **{report['question'][:60]}...**\n"
                    f"   👤 {report['reported_by']['first_name']} | "
                    f"👥 {report['group_name']}\n"
                    f"   🕐 {report_time} | "
                    f"{link_bit}"
                    f"   ID: `{report['_id']}`\n\n"
                )
                
                # Add a button for each report
                keyboard.append([InlineKeyboardButton(f"📋 Review #{i}", callback_data=f"report_back_{report['_id']}")])
            
            if len(pending_reports) > 5:
                response_text += f"... and {len(pending_reports) - 5} more pending reports\n\n"
            
            response_text += f"📈 **Statistics:**\n"
            response_text += f"• Total reports: {len(total_reports)}\n"
            response_text += f"• Pending: {len(pending_reports)}\n"
            response_text += f"• Resolved: {len(total_reports) - len(pending_reports)}\n\n"
            response_text += f"💡 Use `/view <report_id>` to view a specific report"
            
            # Add control buttons
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="view_reports")])
            keyboard.append([InlineKeyboardButton("🗑️ Clear All Resolved", callback_data="clear_resolved_reports")])
            keyboard.append([InlineKeyboardButton("📊 Statistics", callback_data="stats")])
            keyboard.append([InlineKeyboardButton("✅ Close", callback_data="close_report")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(response_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def handle_clear_resolved_reports(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear all resolved reports"""
        query = update.callback_query
        await query.answer()
        
        # Delete all non-pending reports
        result = self.mongo.delete_many('quiz_reports', {'status': {'$ne': 'pending'}})
        
        deleted_count = result.deleted_count if result else 0
        
        await query.edit_message_text(
            f"✅ **Resolved Reports Cleared**\n\n"
            f"🗑️ Deleted {deleted_count} resolved reports.\n"
            f"Only pending reports remain in the database.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Reports", callback_data="view_reports")]])
        )
    
    async def handle_report_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: str):
        """Go back to report view"""
        query = update.callback_query
        await query.answer()
        
        # Get report
        report = self.mongo.find_one('quiz_reports', {'_id': report_id})
        if not report:
            await query.edit_message_text("Report not found.")
            return
        
        # Display the report
        await self.display_report(update, context, report)
    
    async def handle_close_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close the report message"""
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "✅ Report interface closed.\n"
            "Use /start to access the main menu.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]])
        )
    
    async def handle_start_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Go back to start menu"""
        await self.start(update, context)
    
    async def reset_quizzes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command to delete all quizzes"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        if not context.args or context.args[0].lower() != 'confirm':
            await update.message.reply_text(
                "⚠️ **Danger: Reset All Quizzes** ⚠️\n\n"
                "This will delete ALL saved quizzes permanently!\n\n"
                "If you're sure, use:\n"
                "`/reset confirm`\n\n"
                f"📝 Currently have: {len(self.quizzes)} quizzes"
            )
            return
        
        # Delete all quizzes
        deleted_count = len(self.quizzes)
        self.mongo.delete_many('quizzes', {})
        
        # Reset quizzes list
        self.quizzes = []
        self.recently_sent_quizzes = []  # Clear recent tracking
        
        # Reset quiz stats
        self.stats['quizzes_added'] = 0
        self.save_stats()
        
        await update.message.reply_text(
            f"✅ **All Quizzes Reset!**\n\n"
            f"🗑️ Deleted {deleted_count} quizzes\n"
            f"📝 Quiz database is now empty\n\n"
            f"Use /start to add new quizzes!"
        )
    
    async def reset_quizzes_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset quizzes from callback menu"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.callback_query.answer("This command is for admin only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("✅ Confirm Reset", callback_data="confirm_reset")],
            [InlineKeyboardButton("❌ Cancel", callback_data="settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(
            f"⚠️ **Danger: Reset All Quizzes** ⚠️\n\n"
            f"This will delete ALL {len(self.quizzes)} saved quizzes permanently!\n\n"
            f"❌ All quiz data will be lost\n"
            f"❌ Cannot be undone\n"
            f"❌ Groups will stop receiving quizzes\n\n"
            f"Are you absolutely sure?",
            reply_markup=reply_markup
        )
    
    async def confirm_reset_quizzes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute quiz reset"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.callback_query.answer("This command is for admin only.")
            return
        
        # Delete all quizzes
        deleted_count = len(self.quizzes)
        self.mongo.delete_many('quizzes', {})
        
        # Reset quizzes list
        self.quizzes = []
        self.recently_sent_quizzes = []  # Clear recent tracking
        
        # Reset quiz stats
        self.stats['quizzes_added'] = 0
        self.save_stats()
        
        await update.callback_query.edit_message_text(
            f"✅ **All Quizzes Reset Successfully!**\n\n"
            f"🗑️ Deleted {deleted_count} quizzes\n"
            f"📝 Quiz database is now empty\n\n"
            f"Use the menu below to add new quizzes!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")]])
        )
    
    async def set_explanation_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setexplanation command"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        if not context.args:
            current_explanation = self.settings.get('quiz_explanation', "Check back later for results!")
            await update.message.reply_text(
                f"📝 **Current Quiz Explanation:**\n`{current_explanation}`\n\n"
                f"To change the explanation, use:\n"
                f"`/setexplanation Your new explanation text here`\n\n"
                f"💡 This text appears as the explanation in quiz polls."
            )
            return
        
        new_explanation = ' '.join(context.args)
        
        # Update settings
        self.settings['quiz_explanation'] = new_explanation
        self.save_settings()
        
        await update.message.reply_text(
            f"✅ **Quiz Explanation Updated!**\n\n"
            f"New explanation:\n`{new_explanation}`\n\n"
            f"This will be used in all future quiz polls."
        )
    
    async def set_explanation_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set explanation from callback (settings menu)"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.callback_query.answer("This command is for admin only.")
            return
        
        current_explanation = self.settings.get('quiz_explanation', "Check back later for results!")
        
        await update.callback_query.edit_message_text(
            f"📝 **Set Quiz Explanation**\n\n"
            f"Current explanation:\n`{current_explanation}`\n\n"
            f"Please send the new explanation text.\n\n"
            f"💡 This text appears as the explanation in quiz polls."
        )
        
        # Set a flag to expect explanation input
        context.user_data['waiting_for_explanation'] = True
    
    async def handle_explanation_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle explanation input from settings menu"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id) or not context.user_data.get('waiting_for_explanation'):
            return
        
        new_explanation = update.message.text
        
        # Update settings
        self.settings['quiz_explanation'] = new_explanation
        self.save_settings()
        
        context.user_data['waiting_for_explanation'] = False
        
        await update.message.reply_text(
            f"✅ **Quiz Explanation Updated!**\n\n"
            f"New explanation:\n`{new_explanation}`\n\n"
            f"This will be used in all future quiz polls."
        )
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed bot statistics"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        total_quizzes = len(self.quizzes)
        total_groups = len(self.groups)
        total_quizzes_sent = self.stats['total_quizzes_sent']
        quizzes_added = self.stats['quizzes_added']
        manual_quizzes_sent = self.stats.get('manual_quizzes_sent', 0)
        quiz_reports_received = self.stats.get('quiz_reports_received', 0)
        quizzes_deleted_by_reports = self.stats.get('quizzes_deleted_by_reports', 0)
        
        active_groups_count = len([g for g in self.groups if g.get('is_active', True)])
        
        # Calculate active groups (active in last 7 days)
        week_ago = datetime.now() - timedelta(days=7)
        recently_active = len([
            g for g in self.groups 
            if datetime.fromisoformat(g['last_activity']) > week_ago and g.get('is_active', True)
        ])
        
        # Most popular quiz
        most_sent = max(self.quizzes, key=lambda x: x.get('sent_count', 0)) if self.quizzes else None
        
        quiz_interval_hours = self.quiz_interval / 3600
        
        stats_text = (
            f"📊 **Detailed Bot Statistics**\n\n"
            f"📝 **Quizzes Database**\n"
            f"   • Total quizzes: {total_quizzes}\n"
            f"   • Subjects: {len(self.get_subjects())}\n"
            f"   • Quizzes added: {quizzes_added}\n"
            f"   • Most sent quiz: {most_sent['sent_count'] if most_sent else 0} times\n"
            f"   • Quizzes deleted by reports: {quizzes_deleted_by_reports}\n\n"
            
            f"👥 **Groups Analytics**\n"
            f"   • Total groups: {total_groups}\n"
            f"   • Active groups: {active_groups_count}\n"
            f"   • Recently active: {recently_active}\n"
            f"   • Total quizzes sent: {total_quizzes_sent}\n"
            f"   • Manual quizzes sent: {manual_quizzes_sent}\n\n"
            
            f"🎮 **User Quizzes (/quiz)**\n"
            f"   • Sessions started: {self.stats.get('user_quiz_sessions', 0)}\n"
            f"   • Questions served: {self.stats.get('user_quizzes_sent', 0)}\n"
            f"   • Correct answers: {self.stats.get('user_quiz_correct', 0)}\n\n"
            
            f"⚠️ **Quiz Reports**\n"
            f"   • Reports received: {quiz_reports_received}\n"
            f"   • Pending reports: {len(self.mongo.find('quiz_reports', {'status': 'pending'}))}\n"
            f"   • Resolved reports: {len(self.mongo.find('quiz_reports', {'status': {'$ne': 'pending'}}))}\n\n"
            
            f"⏰ **Performance**\n"
            f"   • Bot started: {datetime.fromisoformat(self.stats['bot_start_time']).strftime('%Y-%m-%d %H:%M')}\n"
            f"   • Last quiz sent: {datetime.fromisoformat(self.stats['last_quiz_sent']).strftime('%Y-%m-%d %H:%M') if self.stats['last_quiz_sent'] else 'Never'}\n"
            f"   • Quiz interval: {quiz_interval_hours} hours\n"
            f"   • Next quiz in: ~{quiz_interval_hours} hours\n\n"
            
            f"📈 **Engagement**\n"
            f"   • Avg quizzes per group: {total_quizzes_sent/total_groups if total_groups > 0 else 0:.1f}\n"
            f"   • Total engagement score: {sum(self.stats['group_engagement'].values())}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📋 Export Data", callback_data="export_data")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
            [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
            [InlineKeyboardButton("⚠️ View Reports", callback_data="view_reports")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(stats_text, reply_markup=reply_markup)
    
    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot settings"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        quiz_interval_hours = self.quiz_interval / 3600
        current_explanation = self.settings.get('quiz_explanation', "Check back later for results!")
        
        settings_text = (
            f"⚙️ **Bot Settings**\n\n"
            f"🕐 **Quiz Interval**: {quiz_interval_hours} hours\n"
            f"   - Current delay between random quizzes\n\n"
            f"📝 **Quiz Explanation**:\n`{current_explanation}`\n"
            f"   - Text shown in quiz polls\n\n"
            f"📊 **Database**: {'MongoDB' if self.mongo.is_connected() else 'In-Memory'}\n"
            f"   - Data persistence status\n\n"
            f"📚 **Subjects**: {len(self.get_subjects())}\n"
            f"👥 **Active Groups**: {len([g for g in self.groups if g.get('is_active', True)])}\n"
            f"📝 **Active Quizzes**: {len([q for q in self.quizzes if q.get('is_active', True)])}\n"
            f"🎯 **Manual Quizzes Sent**: {self.stats.get('manual_quizzes_sent', 0)}\n"
            f"⚠️ **Quiz Reports**: {self.stats.get('quiz_reports_received', 0)}\n\n"
            f"💡 Use /setdelay <time> to change the quiz interval\n"
            f"💡 Use /setexplanation to change quiz explanation\n"
            f"💡 Group admins can use /rquiz for immediate quizzes\n"
            f"💡 /rquiz [Subject] [Quiz Folder] sends from a specific location\n"
            f"⚠️ Users can report quizzes with /qreport"
        )
        
        keyboard = [
            [InlineKeyboardButton("🕐 Set Quiz Interval", callback_data="set_interval")],
            [InlineKeyboardButton("📝 Set Explanation", callback_data="set_explanation")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("🔄 Refresh Groups", callback_data="refresh_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
            [InlineKeyboardButton("🗂 Manage Quiz Folders", callback_data="manage_folders")],
            [InlineKeyboardButton("⚠️ View Reports", callback_data="view_reports")],
            [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(settings_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(settings_text, reply_markup=reply_markup)
    
    async def set_quiz_interval_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setdelay command directly"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Please specify the interval.\n\n"
                "**Usage:** `/setdelay <time>`\n\n"
                "**Examples:**\n"
                "• `/setdelay 2h` - 2 hours\n"
                "• `/setdelay 30m` - 30 minutes\n"
                "• `/setdelay 1.5h` - 1.5 hours\n"
                "• `/setdelay 90m` - 90 minutes\n"
                "• `/setdelay 2` - 2 hours (default)\n\n"
                f"**Current interval:** {self.quiz_interval / 3600} hours"
            )
            return
        
        time_input = context.args[0]
        new_interval = self.parse_time_input(time_input)
        
        if new_interval is None:
            await update.message.reply_text(
                "❌ Invalid time format!\n\n"
                "**Valid formats:**\n"
                "• `2h` or `2hr` - 2 hours\n"
                "• `30m` or `30min` - 30 minutes\n"
                "• `1.5h` - 1.5 hours\n"
                "• `90m` - 90 minutes\n"
                "• `2` - 2 hours (default)\n\n"
                f"**Current interval:** {self.quiz_interval / 3600} hours"
            )
            return
        
        if new_interval <= 0:
            await update.message.reply_text("❌ Interval must be greater than 0.")
            return
        
        old_interval = self.quiz_interval
        self.quiz_interval = new_interval
        self.settings['quiz_interval'] = new_interval
        self.save_settings()
        
        # Format display
        if new_interval < 60:
            display_time = f"{new_interval} seconds"
        elif new_interval < 3600:
            display_time = f"{new_interval / 60:.1f} minutes"
        else:
            display_time = f"{new_interval / 3600:.1f} hours"
        
        old_display = f"{old_interval / 3600:.1f} hours" if old_interval >= 3600 else f"{old_interval / 60:.1f} minutes"
        
        await update.message.reply_text(
            f"✅ **Quiz interval updated!**\n\n"
            f"📅 Old interval: {old_display}\n"
            f"📅 New interval: {display_time}\n\n"
            f"Next quiz will be sent in approximately {display_time}."
        )
    
    async def set_quiz_interval_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set quiz interval from callback (settings menu)"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.callback_query.answer("This command is for admin only.")
            return
        
        await update.callback_query.edit_message_text(
            "🕐 **Set Quiz Interval**\n\n"
            "Please send the new interval.\n\n"
            "**Examples:**\n"
            "• `2h` - 2 hours\n"
            "• `30m` - 30 minutes\n"
            "• `1.5h` - 1.5 hours\n"
            "• `90m` - 90 minutes\n"
            "• `2` - 2 hours (default)\n\n"
            "Current interval: {} hours".format(self.quiz_interval / 3600)
        )
        
        # Set a flag to expect interval input
        context.user_data['waiting_for_interval'] = True
    
    async def handle_interval_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quiz interval input from settings menu"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id) or not context.user_data.get('waiting_for_interval'):
            return
        
        time_input = update.message.text
        new_interval = self.parse_time_input(time_input)
        
        if new_interval is None:
            await update.message.reply_text(
                "❌ Invalid time format!\n\n"
                "**Valid formats:**\n"
                "• `2h` or `2hr` - 2 hours\n"
                "• `30m` or `30min` - 30 minutes\n"
                "• `1.5h` - 1.5 hours\n"
                "• `90m` - 90 minutes\n"
                "• `2` - 2 hours (default)\n\n"
                f"**Current interval:** {self.quiz_interval / 3600} hours"
            )
            return
        
        if new_interval <= 0:
            await update.message.reply_text("❌ Interval must be greater than 0.")
            return
        
        old_interval = self.quiz_interval
        self.quiz_interval = new_interval
        self.settings['quiz_interval'] = new_interval
        self.save_settings()
        
        context.user_data['waiting_for_interval'] = False
        
        # Format display
        if new_interval < 60:
            display_time = f"{new_interval} seconds"
        elif new_interval < 3600:
            display_time = f"{new_interval / 60:.1f} minutes"
        else:
            display_time = f"{new_interval / 3600:.1f} hours"
        
        old_display = f"{old_interval / 3600:.1f} hours" if old_interval >= 3600 else f"{old_interval / 60:.1f} minutes"
        
        await update.message.reply_text(
            f"✅ **Quiz interval updated!**\n\n"
            f"📅 Old interval: {old_display}\n"
            f"📅 New interval: {display_time}\n\n"
            f"Next quiz will be sent in approximately {display_time}."
        )
    
    async def start_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start broadcast mode"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        self.broadcast_mode[user_id] = True
        
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        
        message = (
            f"📢 **Broadcast Mode Activated**\n\n"
            f"Please send the message you want to broadcast to all {active_groups} active groups.\n\n"
            f"⚠️ **Warning:** This will send your message to all active groups immediately!\n"
            f"✏️ Type your message now..."
        )
        
        if update.callback_query:
            await update.callback_query.edit_message_text(message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message, reply_markup=reply_markup)
    
    async def send_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
        """Send broadcast message to all groups"""
        user_id = update.effective_user.id
        self.broadcast_mode[user_id] = False
        
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        sent_to = 0
        failed_groups = []
        
        # Send to all active groups
        for group in active_groups:
            try:
                # Don't send broadcasts to private chats
                if group['chat_id'] > 0:
                    continue
                    
                await self.application.bot.send_message(
                    chat_id=group['chat_id'],
                    text=f"📢 **Announcement**\n\n{message_text}\n\n- Admin"
                )
                sent_to += 1
                await asyncio.sleep(0.5)  # Rate limiting
            except Exception as e:
                failed_groups.append(group['title'])
                print(f"Failed to broadcast to {group['title']}: {e}")
                # Mark group as inactive
                group['is_active'] = False
                self.save_group(group)
        
        # Update stats
        self.stats['total_broadcasts_sent'] = self.stats.get('total_broadcasts_sent', 0) + sent_to
        self.save_stats()
        
        # Reload groups after updates
        self.groups = self.load_groups()
        
        # Send report to admin
        report = (
            f"✅ **Broadcast Completed**\n\n"
            f"📤 Sent to: {sent_to}/{len(active_groups)} active groups\n"
            f"✅ Successful: {sent_to}\n"
            f"❌ Failed: {len(failed_groups)}\n"
        )
        
        if failed_groups:
            report += f"\nFailed groups (marked inactive):\n" + "\n".join(failed_groups[:10])
            if len(failed_groups) > 10:
                report += f"\n... and {len(failed_groups) - 10} more"
        
        await update.message.reply_text(report)
    
    async def export_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Export bot data to JSON and CSV files"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        try:
            # Export quizzes to CSV
            if self.quizzes:
                with open('quizzes_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['_id', 'type', 'subject', 'folder', 'question', 'options', 'is_anonymous', 'allows_multiple_answers', 'correct_option_id', 'added_date', 'sent_count', 'manual_sent_count', 'last_sent', 'is_active']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    for quiz in self.quizzes:
                        # Convert options list to string for CSV
                        quiz_export = quiz.copy()
                        quiz_export['options'] = ' | '.join(quiz['options'])
                        writer.writerow(quiz_export)
                
                # Send quizzes CSV
                await context.bot.send_document(
                    chat_id=user_id,
                    document=open('quizzes_export.csv', 'rb'),
                    filename='quizzes_export.csv',
                    caption="📝 Quizzes Export (CSV)"
                )
            
            # Export groups to CSV
            if self.groups:
                with open('groups_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['_id', 'chat_id', 'title', 'added_date', 'member_count', 'quizzes_received', 'manual_quizzes_received', 'last_activity', 'is_active']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    for group in self.groups:
                        writer.writerow(group)
                
                # Send groups CSV
                await context.bot.send_document(
                    chat_id=user_id,
                    document=open('groups_export.csv', 'rb'),
                    filename='groups_export.csv',
                    caption="👥 Groups Export (CSV)"
                )
            
            # Export stats to JSON
            with open('stats_export.json', 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=2, ensure_ascii=False)
            
            # Send stats JSON
            await context.bot.send_document(
                chat_id=user_id,
                document=open('stats_export.json', 'rb'),
                filename='stats_export.json',
                caption="📊 Statistics Export (JSON)"
            )
            
            # Export reports to CSV
            reports = self.mongo.find('quiz_reports', {})
            if reports:
                with open('reports_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['_id', 'status', 'question', 'options', 'correct_option_id', 'reported_by', 'report_time', 'group_name', 'action_taken', 'action_time']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    for report in reports:
                        report_export = report.copy()
                        report_export['options'] = ' | '.join(report['options'])
                        report_export['reported_by'] = f"{report['reported_by']['first_name']} ({report['reported_by']['user_id']})"
                        writer.writerow(report_export)
                
                await context.bot.send_document(
                    chat_id=user_id,
                    document=open('reports_export.csv', 'rb'),
                    filename='reports_export.csv',
                    caption="⚠️ Quiz Reports Export (CSV)"
                )
            
            # Send summary
            summary = (
                f"✅ **Data Export Completed**\n\n"
                f"📁 Files exported:\n"
                f"• quizzes_export.csv ({len(self.quizzes)} quizzes)\n"
                f"• groups_export.csv ({len(self.groups)} groups)\n"
                f"• stats_export.json (statistics)\n"
                f"• reports_export.csv ({len(reports)} reports)\n\n"
                f"💾 All data has been exported successfully!"
            )
            
            if update.callback_query:
                await update.callback_query.edit_message_text(summary)
            else:
                await update.message.reply_text(summary)
                
        except Exception as e:
            error_msg = f"❌ Error exporting data: {str(e)}"
            if update.callback_query:
                await update.callback_query.edit_message_text(error_msg)
            else:
                await update.message.reply_text(error_msg)
    
    async def manage_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show group management interface"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        total_groups = len(self.groups)
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        inactive_groups = total_groups - active_groups
        
        groups_text = (
            f"👥 **Group Management**\n\n"
            f"📊 **Overview**\n"
            f"• Total groups: {total_groups}\n"
            f"• Active groups: {active_groups}\n"
            f"• Inactive groups: {inactive_groups}\n\n"
        )
        
        # Show top 5 most active groups
        active_groups_list = [g for g in self.groups if g.get('is_active', True)]
        sorted_groups = sorted(active_groups_list, key=lambda x: x.get('quizzes_received', 0), reverse=True)[:5]
        
        if sorted_groups:
            groups_text += "🏆 **Top 5 Active Groups:**\n"
            for i, group in enumerate(sorted_groups, 1):
                groups_text += f"{i}. {group['title']} - {group.get('quizzes_received', 0)} auto + {group.get('manual_quizzes_received', 0)} manual quizzes\n"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="manage_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("🔄 Reactivate All", callback_data="reactivate_all")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(groups_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(groups_text, reply_markup=reply_markup)
    
    async def clean_inactive_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove inactive groups"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Find inactive groups
        inactive_groups = [g for g in self.groups if not g.get('is_active', True)]
        
        if not inactive_groups:
            await update.callback_query.answer("No inactive groups found!")
            return
        
        # Remove inactive groups from MongoDB
        for group in inactive_groups:
            self.mongo.delete_one('groups', {'_id': group['_id']})
        
        # Reload groups
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ **Cleaned {len(inactive_groups)} inactive groups**\n\n"
            f"Removed groups that were marked as inactive (likely removed the bot).\n"
            f"Current active groups: {len([g for g in self.groups if g.get('is_active', True)])}"
        )
    
    async def reactivate_all_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reactivate all groups"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Reactivate all groups
        for group in self.groups:
            group['is_active'] = True
            self.save_group(group)
        
        # Reload groups
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ **All groups reactivated!**\n\n"
            f"All {len(self.groups)} groups have been marked as active and will receive quizzes."
        )
    
    async def refresh_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Refresh groups list"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Reload groups from MongoDB
        self.groups = self.load_groups()
        
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        
        await update.callback_query.answer(f"Groups refreshed! {active_groups} active groups loaded.")
    
    async def list_groups_with_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /grouplist command - list all groups with invite links"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        if not self.groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        # Filter out private chats (admin's chat)
        real_groups = [g for g in self.groups if g['chat_id'] < 0]
        
        if not real_groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        active_groups = [g for g in real_groups if g.get('is_active', True)]
        inactive_groups = [g for g in real_groups if not g.get('is_active', True)]
        
        # Show loading message
        loading_msg = await update.message.reply_text("🔄 Fetching group links... This may take a moment.")
        
        groups_text = f"👥 **Groups List ({len(real_groups)} total)**\n\n"
        groups_text += f"🟢 Active: {len(active_groups)}\n"
        groups_text += f"🔴 Inactive: {len(inactive_groups)}\n\n"
        
        all_links_text = "📋 **Group List with Links**\n\n"
        failed_groups = []
        success_count = 0
        
        # Process groups in batches to avoid rate limiting
        for i, group in enumerate(real_groups, 1):
            chat_id = group['chat_id']
            group_title = group.get('title', f"Group {chat_id}")
            status = "🟢" if group.get('is_active', True) else "🔴"
            
            try:
                # Try to get invite link (requires bot to have admin permissions)
                chat = await context.bot.get_chat(chat_id)
                
                try:
                    # Try to create invite link
                    invite_link_obj = await context.bot.create_chat_invite_link(
                        chat_id=chat_id,
                        member_limit=1,
                        expire_date=datetime.now() + timedelta(days=7)
                    )
                    invite_link = invite_link_obj.invite_link
                    link_text = f"[Join {group_title}]({invite_link})"
                except Exception as link_error:
                    # If can't create link, try to export existing link
                    try:
                        invite_link = await context.bot.export_chat_invite_link(chat_id)
                        link_text = f"[Join {group_title}]({invite_link})"
                    except Exception as export_error:
                        link_text = "❌ No invite link (bot needs admin)"
                        invite_link = None
                
                # Add to detailed list
                all_links_text += f"{i}. {status} **{group_title}**\n"
                all_links_text += f"   • ID: `{chat_id}`\n"
                all_links_text += f"   • Link: {link_text}\n"
                all_links_text += f"   • Auto Quizzes: {group.get('quizzes_received', 0)}\n"
                all_links_text += f"   • Manual Quizzes: {group.get('manual_quizzes_received', 0)}\n"
                
                if invite_link:
                    success_count += 1
                
                all_links_text += "\n"
                
                # Add to summary text
                groups_text += f"{i}. {status} **{group_title}**\n"
                if invite_link:
                    groups_text += f"   🔗 {invite_link}\n"
                groups_text += f"   📊 Auto: {group.get('quizzes_received', 0)} | Manual: {group.get('manual_quizzes_received', 0)}\n\n"
                
            except Exception as e:
                # Group not accessible or bot removed
                failed_groups.append(group_title)
                all_links_text += f"{i}. 🔴 **{group_title}** (❌ Bot not in group)\n"
                all_links_text += f"   • ID: `{chat_id}`\n"
                all_links_text += f"   • Last active: {group.get('last_activity', 'Never')[:10]}\n\n"
                
                groups_text += f"{i}. 🔴 **{group_title}** (Bot removed)\n\n"
                
                # Mark as inactive
                group['is_active'] = False
                self.save_group(group)
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.1)
        
        # Reload groups after updates
        self.groups = self.load_groups()
        
        # Update loading message with summary
        await loading_msg.delete()
        
        # Send summary first
        summary_text = (
            f"📊 **Groups Summary**\n\n"
            f"✅ Successfully fetched links: {success_count}/{len(real_groups)}\n"
            f"❌ Failed/Inaccessible: {len(failed_groups)}\n"
            f"🟢 Active groups: {len(active_groups)}\n"
            f"🔴 Inactive groups: {len(inactive_groups)}\n\n"
        )
        
        if failed_groups:
            summary_text += "❌ **Failed Groups (Bot not in group):**\n"
            for group in failed_groups[:5]:  # Show only first 5
                summary_text += f"• {group}\n"
            if len(failed_groups) > 5:
                summary_text += f"... and {len(failed_groups) - 5} more\n"
            summary_text += "\n"
        
        # Add instructions
        summary_text += (
            "📝 **Note:** Links expire in 7 days\n"
            "🔄 Use /refreshgroups to update group status\n"
            "🗑️ Inactive groups are automatically cleaned"
        )
        
        # Create inline keyboard for navigation
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh List", callback_data="refresh_groups")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("📊 All Group Stats", callback_data="manage_groups")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(summary_text, reply_markup=reply_markup)
        
        # Check if detailed list is too long for Telegram
        if len(all_links_text) > 4000:
            # Split into multiple messages
            chunks = [all_links_text[i:i+4000] for i in range(0, len(all_links_text), 4000)]
            for i, chunk in enumerate(chunks[:3]):  # Send max 3 chunks
                if i == 0:
                    await update.message.reply_text(chunk, parse_mode='Markdown')
                else:
                    await update.message.reply_text(f"... (continued)\n\n{chunk}", parse_mode='Markdown')
                await asyncio.sleep(0.5)
            
            if len(chunks) > 3:
                await update.message.reply_text(f"📝 And {len(chunks)-3} more parts... List truncated.")
        else:
            # Send complete list
            await update.message.reply_text(all_links_text, parse_mode='Markdown')
    
    async def quick_groups_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /groups command - quick list of groups without links"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        # Filter out private chats (admin's chat)
        real_groups = [g for g in self.groups if g['chat_id'] < 0]
        
        if not real_groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        active_groups = [g for g in real_groups if g.get('is_active', True)]
        inactive_groups = [g for g in real_groups if not g.get('is_active', True)]
        
        groups_text = f"👥 **Groups Summary ({len(real_groups)} total)**\n\n"
        
        if active_groups:
            groups_text += f"🟢 **Active Groups ({len(active_groups)})**\n"
            for i, group in enumerate(active_groups[:20], 1):  # Show only first 20
                groups_text += f"{i}. {group.get('title', 'Unknown')} (ID: `{group['chat_id']}`)\n"
                groups_text += f"   📊 Auto: {group.get('quizzes_received', 0)} | Manual: {group.get('manual_quizzes_received', 0)}\n"
            
            if len(active_groups) > 20:
                groups_text += f"... and {len(active_groups) - 20} more\n"
            
            groups_text += "\n"
        
        if inactive_groups:
            groups_text += f"🔴 **Inactive Groups ({len(inactive_groups)})**\n"
            for i, group in enumerate(inactive_groups[:10], 1):  # Show only first 10
                groups_text += f"{i}. {group.get('title', 'Unknown')} (ID: `{group['chat_id']}`)\n"
            
            if len(inactive_groups) > 10:
                groups_text += f"... and {len(inactive_groups) - 10} more\n"
            
            groups_text += "\n"
        
        groups_text += (
            f"📊 **Stats:**\n"
            f"• Total quizzes sent to all groups: {self.stats.get('total_quizzes_sent', 0)}\n"
            f"• Manual quizzes sent: {self.stats.get('manual_quizzes_sent', 0)}\n"
            f"• Active groups percentage: {(len(active_groups)/len(real_groups)*100 if real_groups else 0):.1f}%\n\n"
            f"💡 Use `/grouplist` for detailed list with invite links\n"
            f"💡 Use `/grouplinks` for only links (export format)"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔗 Get Links", callback_data="get_group_links")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="manage_groups")],
            [InlineKeyboardButton("📊 Full Stats", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(groups_text, reply_markup=reply_markup)
    
    async def export_group_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /grouplinks command - export group links in simple format"""
        user_id = update.effective_user.id
        
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        # Filter out private chats (admin's chat)
        real_groups = [g for g in self.groups if g['chat_id'] < 0]
        
        if not real_groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        loading_msg = await update.message.reply_text("🔄 Generating group links...")
        
        links_text = "🔗 **Group Invite Links**\n\n"
        links_only = "📋 **Links Only (for export):**\n\n"
        
        success_count = 0
        
        for group in real_groups:
            if not group.get('is_active', True):
                continue
                
            chat_id = group['chat_id']
            group_title = group.get('title', f"Group {chat_id}")
            
            try:
                # Try to create invite link
                try:
                    invite_link_obj = await context.bot.create_chat_invite_link(
                        chat_id=chat_id,
                        member_limit=1,
                        expire_date=datetime.now() + timedelta(days=7)
                    )
                    invite_link = invite_link_obj.invite_link
                except:
                    # Try to export existing link
                    invite_link = await context.bot.export_chat_invite_link(chat_id)
                
                links_text += f"• **{group_title}**\n{invite_link}\n\n"
                links_only += f"{invite_link}\n"
                success_count += 1
                
            except Exception as e:
                links_text += f"• **{group_title}** - ❌ No link available\n\n"
            
            await asyncio.sleep(0.1)
        
        await loading_msg.delete()
        
        summary = (
            f"✅ **Group Links Export**\n\n"
            f"📊 Generated {success_count} links from {len(real_groups)} groups\n"
            f"⏰ Links expire in 7 days\n"
            f"📋 Copy links from below section\n\n"
            f"💡 **Tip:** Use `/grouplist` for detailed view\n"
            f"💡 **Tip:** Use `/groups` for quick overview"
        )
        
        await update.message.reply_text(summary)
        
        # Send links text (might be long)
        if len(links_text) > 4000:
            chunks = [links_text[i:i+4000] for i in range(0, len(links_text), 4000)]
            for chunk in chunks[:3]:
                await update.message.reply_text(chunk, parse_mode='Markdown')
                await asyncio.sleep(0.5)
        else:
            await update.message.reply_text(links_text, parse_mode='Markdown')
        
        # Send links-only section
        await update.message.reply_text("📋 **Copy-paste section:**")
        if len(links_only) > 4000:
            # Save to file if too long
            with open('group_links.txt', 'w', encoding='utf-8') as f:
                f.write(links_only)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=open('group_links.txt', 'rb'),
                filename='group_links.txt',
                caption="📋 Group links (text file)"
            )
        else:
            await update.message.reply_text(f"```\n{links_only}\n```", parse_mode='Markdown')
    
    # NEW: Add sudo user command (only main admin can use)
    async def add_sudo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /addsudo <user_id> command - add a new sudo user"""
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Only the main bot admin can add sudo users.")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a user ID.\n\n"
                "**Usage:** `/addsudo <user_id>`\n\n"
                "You can get a user's ID by having them send any message to the bot."
            )
            return
        
        try:
            new_sudo_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Please provide a numeric ID.")
            return
        
        if new_sudo_id == ADMIN_USER_ID:
            await update.message.reply_text("❌ The main admin is already a super admin.")
            return
        
        if new_sudo_id in self.sudo_users:
            await update.message.reply_text(f"❌ User `{new_sudo_id}` is already a sudo user.")
            return
        
        # Save to database
        self.save_sudo_user(new_sudo_id)
        
        await update.message.reply_text(
            f"✅ **Sudo user added!**\n\n"
            f"User ID: `{new_sudo_id}`\n\n"
            f"This user can now use all admin commands."
        )
    
    # NEW: Remove sudo user command (only main admin can use)
    async def remove_sudo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /remsudo <user_id> command - remove a sudo user"""
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Only the main bot admin can remove sudo users.")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a user ID.\n\n"
                "**Usage:** `/remsudo <user_id>`"
            )
            return
        
        try:
            sudo_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Please provide a numeric ID.")
            return
        
        if sudo_id not in self.sudo_users:
            await update.message.reply_text(f"❌ User `{sudo_id}` is not a sudo user.")
            return
        
        # Remove from database
        self.remove_sudo_user(sudo_id)
        
        await update.message.reply_text(
            f"✅ **Sudo user removed!**\n\n"
            f"User ID: `{sudo_id}`\n\n"
            f"This user no longer has admin privileges."
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "stats":
            await self.show_stats(update, context)
        elif data == "add_quiz":
            # NEW: Add Quiz now starts the hierarchical Subject → Folder flow
            text, keyboard = self.build_admin_subject_menu()
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings":
            await self.show_settings(update, context)
        elif data == "broadcast":
            await self.start_broadcast(update, context)
        elif data == "manage_groups":
            await self.manage_groups(update, context)
        elif data == "export_data":
            await self.export_data(update, context)
        elif data == "reset_quizzes":
            await self.reset_quizzes_callback(update, context)
        elif data == "confirm_reset":
            await self.confirm_reset_quizzes(update, context)
        elif data == "set_interval":
            await self.set_quiz_interval_callback(update, context)
        elif data == "set_explanation":
            await self.set_explanation_callback(update, context)
        elif data == "cancel_broadcast":
            user_id = query.from_user.id
            self.broadcast_mode[user_id] = False
            await query.edit_message_text("❌ Broadcast cancelled.")
        elif data == "clean_inactive":
            await self.clean_inactive_groups(update, context)
        elif data == "reactivate_all":
            await self.reactivate_all_groups(update, context)
        elif data == "refresh_groups":
            await self.refresh_groups(update, context)
        elif data == "get_group_links":
            await self.export_group_links(update, context)
        elif data.startswith("remove_group_"):
            chat_id = int(data.split("_")[2])
            await self.remove_group(update, context, chat_id)
        elif data.startswith("group_stats_"):
            chat_id = int(data.split("_")[2])
            await self.show_group_stats(update, context, chat_id)
        elif data.startswith("delete_quiz_"):
            report_id = data[12:]  # Remove "delete_quiz_" prefix
            await self.handle_delete_quiz(update, context, report_id)
        elif data.startswith("delete_similar_"):
            report_id = data[15:]  # Remove "delete_similar_" prefix
            await self.handle_delete_similar_quizzes(update, context, report_id)
        elif data.startswith("ignore_report_"):
            report_id = data[14:]  # Remove "ignore_report_" prefix
            await self.handle_ignore_report(update, context, report_id)
        elif data.startswith("view_similar_"):
            report_id = data[13:]  # Remove "view_similar_" prefix
            await self.handle_view_similar(update, context, report_id)
        # NEW: Edit / Replace a reported quiz
        elif data.startswith("edit_quiz_"):
            report_id = data[len("edit_quiz_"):]
            await self.handle_edit_quiz_menu(update, context, report_id)
        elif data.startswith("editq_pick_"):
            token = data[len("editq_pick_"):]
            await self.handle_editq_pick(update, context, token)
        elif data.startswith("editq_text_"):
            token = data[len("editq_text_"):]
            await self.handle_editq_text(update, context, token)
        elif data.startswith("editq_poll_"):
            token = data[len("editq_poll_"):]
            await self.handle_editq_poll(update, context, token)
        elif data == "view_reports":
            await self.handle_view_reports(update, context)
        elif data == "clear_resolved_reports":
            await self.handle_clear_resolved_reports(update, context)
        elif data.startswith("report_back_"):
            report_id = data[12:]  # Remove "report_back_" prefix
            await self.handle_report_back(update, context, report_id)
        elif data == "close_report":
            await self.handle_close_report(update, context)
        elif data == "start_menu":
            await self.handle_start_menu(update, context)
        # ==========================================================
        # NEW: 🗂 Manage Quiz Folders (admin)
        # ==========================================================
        elif data == "manage_folders" or data == "mf_home":
            await self.show_manage_folders(update, context)
        elif data == "mf_subjview":
            await self.show_manage_subjects(update, context)
        elif data == "mf_newsubj":
            context.user_data['await'] = 'new_subject'
            await query.edit_message_text(
                "➕ Create New Subject\n\n"
                "Send the name of the new subject now.\n\n"
                "After this you will pick (or create) a quiz folder, "
                "then you can send Quiz Mode polls to fill it."
            )
        elif data == "mf_newfold":
            subject = context.user_data.get('manage_subject')
            if not subject:
                await query.edit_message_text(
                    "❌ Please open a subject first, then create a quiz folder inside it.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 View Subjects", callback_data="mf_subjview")]])
                )
            else:
                context.user_data['await'] = 'new_folder'
                await query.edit_message_text(
                    f"➕ Create New Quiz Folder\n\n"
                    f"📚 Subject: {subject}\n\n"
                    f"Send the name of the new quiz folder now.\n\n"
                    f"You will then be able to send Quiz Mode polls to fill it."
                )
        elif data.startswith("mf_subj_"):
            token = data[8:]
            subject = self.resolve_subject_token(token)
            if not subject:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                await self.show_manage_subject_detail(update, context, subject)
        elif data.startswith("mf_fold_"):
            token = data[8:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                await self.show_manage_folder_detail(update, context, pair[0], pair[1])
        elif data.startswith("mf_rensub_"):
            token = data[10:]
            subject = self.resolve_subject_token(token)
            if not subject:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                context.user_data['await'] = 'rename_subject'
                context.user_data['rename_target'] = {'type': 'subject', 'subject': subject}
                await query.edit_message_text(
                    f"✏️ Rename Subject\n\n"
                    f"Current name: {subject}\n\n"
                    f"Send the new name now."
                )
        elif data.startswith("mf_renfold_"):
            token = data[11:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                context.user_data['await'] = 'rename_folder'
                context.user_data['rename_target'] = {'type': 'folder', 'subject': pair[0], 'folder': pair[1]}
                await query.edit_message_text(
                    f"✏️ Rename Quiz Folder\n\n"
                    f"📚 Subject: {pair[0]}\n"
                    f"Current folder: {pair[1]}\n\n"
                    f"Send the new name now."
                )
        elif data.startswith("mf_delsub_"):
            token = data[10:]
            subject = self.resolve_subject_token(token)
            if not subject:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                count = self.mongo.count_documents('quizzes', {'subject': subject})
                await query.edit_message_text(
                    f"⚠️ Delete Subject\n\n"
                    f"📚 Subject: {subject}\n"
                    f"📝 Quizzes inside (all its folders): {count}\n\n"
                    f"Deleting this subject will PERMANENTLY DELETE {count} quiz question(s).\n"
                    f"This cannot be undone!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"🗑️ Confirm Delete ({count} quizzes)", callback_data=f"mf_cdelsub_{token}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"mf_subj_{token}")]
                    ])
                )
        elif data.startswith("mf_cdelsub_"):
            token = data[11:]
            subject = self.resolve_subject_token(token)
            if not subject:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                result = self.mongo.delete_many('quizzes', {'subject': subject})
                deleted = result.deleted_count if result else 0
                self.quizzes = self.load_quizzes()
                await query.edit_message_text(
                    f"✅ Subject Deleted\n\n"
                    f"📚 {subject}\n"
                    f"🗑️ {deleted} quiz question(s) removed permanently.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📚 View Subjects", callback_data="mf_subjview")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
                    ])
                )
        elif data.startswith("mf_delfold_"):
            token = data[11:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                subject, folder = pair
                count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder})
                await query.edit_message_text(
                    f"⚠️ Delete Quiz Folder\n\n"
                    f"📚 Subject: {subject}\n"
                    f"📁 Folder: {folder}\n"
                    f"📝 Quizzes inside: {count}\n\n"
                    f"Deleting this folder will PERMANENTLY DELETE {count} quiz question(s).\n"
                    f"This cannot be undone!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"🗑️ Confirm Delete ({count} quizzes)", callback_data=f"mf_cdelfold_{token}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"mf_subj_{self.register_subject_token(subject)}")]
                    ])
                )
        elif data.startswith("mf_cdelfold_"):
            token = data[12:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                subject, folder = pair
                result = self.mongo.delete_many('quizzes', {'subject': subject, 'folder': folder})
                deleted = result.deleted_count if result else 0
                self.quizzes = self.load_quizzes()
                await query.edit_message_text(
                    f"✅ Quiz Folder Deleted\n\n"
                    f"📚 {subject} → 📁 {folder}\n"
                    f"🗑️ {deleted} quiz question(s) removed permanently.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📚 Back to Subject", callback_data=f"mf_subj_{self.register_subject_token(subject)}")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
                    ])
                )
        elif data.startswith("mf_addhere_"):
            token = data[11:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                self.set_admin_selection(context, pair[0], pair[1])
                await self.enter_adding_mode(update, context)
        # ==========================================================
        # NEW: Manage Quiz Folders — sub-folder support
        # ==========================================================
        elif data.startswith("mf_subf_"):
            token = data[8:]
            triple = self.resolve_qz_ctx(token)
            if not triple:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                await self.show_manage_subfolder_detail(update, context, triple[0], triple[1], triple[2])
        elif data.startswith("mf_newsubf_"):
            token = data[11:]
            pair = self.resolve_pair_token(token)
            if not pair:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                context.user_data['await'] = 'new_subfolder'
                context.user_data['new_subfolder_pair'] = {'subject': pair[0], 'folder': pair[1]}
                await query.edit_message_text(
                    f"➕ Create Sub-folder\n\n"
                    f"📚 Subject: {pair[0]}\n📁 Quiz Folder: {pair[1]}\n\n"
                    f"Send the name of the new sub-folder now.\n\n"
                    f"You'll be switched into quiz-saving mode for it right away."
                )
        elif data.startswith("mf_addheresubf_"):
            token = data[15:]
            triple = self.resolve_qz_ctx(token)
            if not triple:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                self.set_admin_selection(context, triple[0], triple[1], triple[2])
                await self.enter_adding_mode(update, context)
        elif data.startswith("mf_cdelsubf_"):
            token = data[12:]
            triple = self.resolve_qz_ctx(token)
            if not triple:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                subject, folder, subfolder = triple
                result = self.mongo.delete_many('quizzes', {'subject': subject, 'folder': folder, 'subfolder': subfolder})
                deleted = result.deleted_count if result else 0
                self.quizzes = self.load_quizzes()
                pair_token = self.register_pair_token(subject, folder)
                await query.edit_message_text(
                    f"✅ Sub-folder Deleted\n\n"
                    f"📂 {subfolder}\n"
                    f"🗑️ {deleted} quiz question(s) removed permanently.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📁 Back to Folder", callback_data=f"mf_fold_{pair_token}")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")]
                    ])
                )
        elif data.startswith("mf_delsubf_"):
            token = data[11:]
            triple = self.resolve_qz_ctx(token)
            if not triple:
                await self.show_manage_subjects(update, context, notice="⚠️ Button expired — list refreshed.")
            else:
                subject, folder, subfolder = triple
                count = self.mongo.count_documents('quizzes', {'subject': subject, 'folder': folder, 'subfolder': subfolder})
                await query.edit_message_text(
                    f"⚠️ Delete Sub-folder\n\n"
                    f"📚 Subject: {subject}\n📁 Quiz Folder: {folder}\n📂 Sub-folder: {subfolder}\n"
                    f"📝 Quizzes inside: {count}\n\n"
                    f"Deleting this sub-folder will PERMANENTLY DELETE {count} quiz question(s).\n"
                    f"This cannot be undone!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"🗑️ Confirm Delete ({count} quizzes)", callback_data=f"mf_cdelsubf_{token}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"mf_subf_{token}")]
                    ])
                )
        # ==========================================================
        # NEW: Admin Add Quiz flow (Subject → Folder → Polls → Done)
        # ==========================================================
        elif data == "addquiz_newsubj":
            context.user_data['await'] = 'new_subject'
            await query.edit_message_text(
                "➕ Create New Subject\n\n"
                "Send the name of the new subject now."
            )
        elif data.startswith("addquiz_subj_"):
            token = data[13:]
            subject = self.resolve_subject_token(token)
            if not subject:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Button expired — list refreshed.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                context.user_data['add_state'] = {'subject': subject, 'folder': None, 'saved_count': 0}
                text, keyboard = self.build_admin_folder_menu(subject)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "addquiz_backsubj":
            text, keyboard = self.build_admin_subject_menu()
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "addquiz_newfold":
            add_state = context.user_data.get('add_state') or {}
            subject = add_state.get('subject') or context.user_data.get('manage_subject')
            if not subject:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Please select a subject first.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                context.user_data['await'] = 'new_folder'
                await query.edit_message_text(
                    f"➕ Create New Quiz Folder\n\n"
                    f"📚 Subject: {subject}\n\n"
                    f"Send the name of the new quiz folder now."
                )
        elif data.startswith("addquiz_fold_"):
            token = data[13:]
            pair = self.resolve_pair_token(token)
            if not pair:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Button expired — list refreshed.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # NEW: sub-folder (optional) step before entering adding mode
                text, keyboard = self.build_admin_subfolder_menu(pair[0], pair[1])
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("addquiz_subf_"):
            token = data[13:]
            triple = self.resolve_qz_ctx(token)
            if not triple:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Button expired — list refreshed.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                self.set_admin_selection(context, triple[0], triple[1], triple[2])
                await self.enter_adding_mode(update, context)
        elif data.startswith("addquiz_nosubf_"):
            token = data[15:]
            pair = self.resolve_pair_token(token)
            if not pair:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Button expired — list refreshed.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                self.set_admin_selection(context, pair[0], pair[1], '')
                await self.enter_adding_mode(update, context)
        elif data.startswith("addquiz_newsubf_"):
            token = data[16:]
            pair = self.resolve_pair_token(token)
            if not pair:
                text, keyboard = self.build_admin_subject_menu()
                await query.edit_message_text(
                    "⚠️ Button expired — list refreshed.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                context.user_data['await'] = 'new_subfolder'
                context.user_data['new_subfolder_pair'] = {'subject': pair[0], 'folder': pair[1]}
                await query.edit_message_text(
                    f"➕ Create New Sub-folder\n\n"
                    f"📚 Subject: {pair[0]}\n📁 Quiz Folder: {pair[1]}\n\n"
                    f"Send the name of the new sub-folder now."
                )
        elif data == "addquiz_done":
            await self.finish_adding(update, context)
        # ==========================================================
        # NEW: User /quiz browsing & sessions
        # ==========================================================
        elif data.startswith("qz_subj_"):
            await self.handle_qz_subject(update, context, data[8:])
        elif data.startswith("qz_fold_"):
            await self.handle_qz_folder(update, context, data[8:])
        elif data.startswith("qz_subf_"):
            await self.handle_qz_subfolder(update, context, data[8:])
        elif data.startswith("qz_pickcount_"):
            await self.handle_qz_pickcount(update, context, data[13:])
        elif data.startswith("qz_cntcustom_"):
            await self.handle_qz_count_custom(update, context, data[13:])
        elif data.startswith("qz_cnt_"):
            # format: qz_cnt_<ctx_token>_<count>
            body = data[len("qz_cnt_"):]
            ctx_token, _, count_str = body.rpartition('_')
            await self.handle_qz_count_chosen(update, context, ctx_token, int(count_str))
        elif data.startswith("qz_tmrcustom_"):
            # format: qz_tmrcustom_<ctx_token>_<count>
            body = data[len("qz_tmrcustom_"):]
            ctx_token, _, count_str = body.rpartition('_')
            await self.handle_qz_timer_custom(update, context, ctx_token, int(count_str))
        elif data.startswith("qz_tmr_"):
            # format: qz_tmr_<ctx_token>_<count>_<secs>
            body = data[len("qz_tmr_"):]
            rest, _, secs_str = body.rpartition('_')
            ctx_token, _, count_str = rest.rpartition('_')
            await self.handle_qz_timer_chosen(update, context, ctx_token, int(count_str), int(secs_str))
        elif data == "qz_restart":
            await self.handle_qz_restart(update, context)
        elif data == "qz_back_subjects":
            text, keyboard = self.build_user_subject_menu()
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qz_back_folders":
            subject = context.user_data.get('quiz_browse_subject')
            if not subject:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                text, keyboard = self.build_user_folder_menu(subject)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        # ==========================================================
        # NEW: Multi-chapter quiz selection (pick several folders at once)
        # ==========================================================
        elif data.startswith("qzm_mode_"):
            token = data[len("qzm_mode_"):]
            subject = self.resolve_subject_token(token)
            if not subject:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # Keep existing selection only if the user is still on the same subject
                if context.user_data.get('quiz_multi_subject') != subject:
                    context.user_data['quiz_multi_selected'] = set()
                    context.user_data['quiz_multi_subject'] = subject
                context.user_data['quiz_browse_subject'] = subject
                selected = context.user_data.get('quiz_multi_selected', set())
                text, keyboard = self.build_user_folder_multi_menu(subject, selected)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_tgl_"):
            token = data[len("qzm_tgl_"):]
            pair = self.resolve_pair_token(token)
            if not pair:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subject, folder = pair
                selected = context.user_data.setdefault('quiz_multi_selected', set())
                if folder in selected:
                    selected.discard(folder)
                else:
                    selected.add(folder)
                context.user_data['quiz_multi_subject'] = subject
                context.user_data['quiz_browse_subject'] = subject
                text, keyboard = self.build_user_folder_multi_menu(subject, selected)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_all_"):
            token = data[len("qzm_all_"):]
            subject = self.resolve_subject_token(token)
            if subject:
                folders = self.get_structure()['folders'].get(subject, {})
                context.user_data['quiz_multi_selected'] = set(folders.keys())
                context.user_data['quiz_multi_subject'] = subject
                text, keyboard = self.build_user_folder_multi_menu(subject, context.user_data['quiz_multi_selected'])
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_clr_"):
            token = data[len("qzm_clr_"):]
            subject = self.resolve_subject_token(token)
            if subject:
                context.user_data['quiz_multi_selected'] = set()
                context.user_data['quiz_multi_subject'] = subject
                text, keyboard = self.build_user_folder_multi_menu(subject, set())
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_start_"):
            token = data[len("qzm_start_"):]
            subject = self.resolve_subject_token(token)
            selected = context.user_data.get('quiz_multi_selected', set())
            if not subject:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            elif not selected:
                text, keyboard = self.build_user_folder_multi_menu(subject, selected)
                await query.edit_message_text(
                    "⚠️ Please select at least one chapter first.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                folders = sorted(selected)
                ctx_token = self.register_multi_ctx(subject, folders)
                text, keyboard = self.build_multi_quiz_count_menu(subject, folders, ctx_token)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_backcnt_"):
            token = data[len("qzm_backcnt_"):]
            pair = self.resolve_multi_ctx(token)
            if not pair:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subject, folders = pair
                text, keyboard = self.build_multi_quiz_count_menu(subject, list(folders), token)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_cntcustom_"):
            token = data[len("qzm_cntcustom_"):]
            pair = self.resolve_multi_ctx(token)
            if not pair:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subject, folders = pair
                query_filter = {'subject': subject, 'folder': {'$in': list(folders)}, 'is_active': True}
                total = self.mongo.count_documents('quizzes', query_filter)
                context.user_data['await'] = 'quiz_multi_custom_count'
                context.user_data['quiz_multi_ctx_token'] = token
                await query.edit_message_text(f"✏️ Send the number of questions you want (1-{total}).")
        elif data.startswith("qzm_cnt_"):
            # format: qzm_cnt_<ctx_token>_<count>
            body = data[len("qzm_cnt_"):]
            ctx_token, _, count_str = body.rpartition('_')
            count = int(count_str)
            text, keyboard = self.build_multi_quiz_timer_menu(ctx_token, count)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzm_tmrcustom_"):
            # format: qzm_tmrcustom_<ctx_token>_<count>
            body = data[len("qzm_tmrcustom_"):]
            ctx_token, _, count_str = body.rpartition('_')
            context.user_data['await'] = 'quiz_multi_custom_timer'
            context.user_data['quiz_multi_ctx_token'] = ctx_token
            context.user_data['quiz_multi_setup_count'] = int(count_str)
            await query.edit_message_text("✏️ Send the time limit per question, in seconds (5-600).")
        elif data.startswith("qzm_tmr_"):
            # format: qzm_tmr_<ctx_token>_<count>_<secs>
            body = data[len("qzm_tmr_"):]
            rest, _, secs_str = body.rpartition('_')
            ctx_token, _, count_str = rest.rpartition('_')
            pair = self.resolve_multi_ctx(ctx_token)
            if not pair:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subject, folders = pair
                await self.launch_quiz_session_multi(
                    context, query.message.chat_id, subject, list(folders),
                    int(count_str), int(secs_str), edit_query=query)
        # ==========================================================
        # NEW: Multi-SUBJECT quiz selection (pick several subjects/quiz-lists at once)
        # ==========================================================
        elif data == "qzsm_mode":
            selected = context.user_data.get('quiz_multi_subjects_selected', set())
            text, keyboard = self.build_user_subject_multi_menu(selected)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzsm_tgl_"):
            token = data[len("qzsm_tgl_"):]
            subject = self.resolve_subject_token(token)
            if not subject:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                selected = context.user_data.setdefault('quiz_multi_subjects_selected', set())
                if subject in selected:
                    selected.discard(subject)
                else:
                    selected.add(subject)
                text, keyboard = self.build_user_subject_multi_menu(selected)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsm_all":
            structure = self.get_structure()
            selected = set(structure['subjects'].keys())
            context.user_data['quiz_multi_subjects_selected'] = selected
            text, keyboard = self.build_user_subject_multi_menu(selected)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsm_clr":
            context.user_data['quiz_multi_subjects_selected'] = set()
            text, keyboard = self.build_user_subject_multi_menu(set())
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsm_start":
            # NEW: "Next" — instead of starting immediately, move on to multi-chapter selection
            selected = context.user_data.get('quiz_multi_subjects_selected', set())
            if not selected:
                text, keyboard = self.build_user_subject_multi_menu(selected)
                await query.edit_message_text(
                    "⚠️ Please select at least one subject first.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subjects = sorted(selected)
                context.user_data['quiz_subjmulti_subjects'] = subjects
                context.user_data['quiz_subjmulti_chapters_selected'] = set()
                text, keyboard = self.build_subjmulti_chapter_menu(subjects, set())
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        # ==========================================================
        # NEW: Multi-SUBJECT → Multi-CHAPTER (step 2 of the combined flow)
        # ==========================================================
        elif data.startswith("qzsmch_tgl_"):
            token = data[len("qzsmch_tgl_"):]
            name = self.resolve_name_token(token)
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            if not name or not subjects:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                selected = context.user_data.setdefault('quiz_subjmulti_chapters_selected', set())
                if name in selected:
                    selected.discard(name)
                else:
                    selected.add(name)
                text, keyboard = self.build_subjmulti_chapter_menu(subjects, selected)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmch_all":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = self.get_union_folders(subjects)
            context.user_data['quiz_subjmulti_chapters_selected'] = set(folders.keys())
            text, keyboard = self.build_subjmulti_chapter_menu(subjects, context.user_data['quiz_subjmulti_chapters_selected'])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmch_clr":
            context.user_data['quiz_subjmulti_chapters_selected'] = set()
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            text, keyboard = self.build_subjmulti_chapter_menu(subjects, set())
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmch_show":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            selected = context.user_data.get('quiz_subjmulti_chapters_selected', set())
            text, keyboard = self.build_subjmulti_chapter_menu(subjects, selected)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmch_next":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            selected = context.user_data.get('quiz_subjmulti_chapters_selected', set())
            if not subjects:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            elif not selected:
                text, keyboard = self.build_subjmulti_chapter_menu(subjects, selected)
                await query.edit_message_text(
                    "⚠️ Please select at least one chapter first.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                folders = sorted(selected)
                subfolder_options = self.get_union_subfolders(subjects, folders)
                if subfolder_options:
                    context.user_data['quiz_subjmulti_had_subfolders'] = True
                    context.user_data['quiz_subjmulti_subfolders_selected'] = set()
                    text, keyboard = self.build_subjmulti_subfolder_menu(subjects, folders, set())
                else:
                    context.user_data['quiz_subjmulti_had_subfolders'] = False
                    ctx_token = self.register_subjmulti_full_ctx(subjects, folders, None)
                    text, keyboard = self.build_subjmulti_full_count_menu(subjects, folders, None, ctx_token, "qzsmch_show")
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        # ==========================================================
        # NEW: Multi-SUBJECT → Multi-CHAPTER → Multi-SUB-FOLDER (step 3 of the combined flow)
        # ==========================================================
        elif data.startswith("qzsmsf_tgl_"):
            token = data[len("qzsmsf_tgl_"):]
            name = self.resolve_name_token(token)
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = sorted(context.user_data.get('quiz_subjmulti_chapters_selected', set()))
            if not name or not subjects or not folders:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ This button is no longer valid (data may have changed).\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                selected = context.user_data.setdefault('quiz_subjmulti_subfolders_selected', set())
                if name in selected:
                    selected.discard(name)
                else:
                    selected.add(name)
                text, keyboard = self.build_subjmulti_subfolder_menu(subjects, folders, selected)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmsf_all":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = sorted(context.user_data.get('quiz_subjmulti_chapters_selected', set()))
            names = self.get_union_subfolders(subjects, folders)
            context.user_data['quiz_subjmulti_subfolders_selected'] = set(names)
            text, keyboard = self.build_subjmulti_subfolder_menu(subjects, folders, context.user_data['quiz_subjmulti_subfolders_selected'])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmsf_clr":
            context.user_data['quiz_subjmulti_subfolders_selected'] = set()
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = sorted(context.user_data.get('quiz_subjmulti_chapters_selected', set()))
            text, keyboard = self.build_subjmulti_subfolder_menu(subjects, folders, set())
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmsf_show":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = sorted(context.user_data.get('quiz_subjmulti_chapters_selected', set()))
            selected = context.user_data.get('quiz_subjmulti_subfolders_selected', set())
            text, keyboard = self.build_subjmulti_subfolder_menu(subjects, folders, selected)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "qzsmsf_start":
            subjects = context.user_data.get('quiz_subjmulti_subjects') or []
            folders = sorted(context.user_data.get('quiz_subjmulti_chapters_selected', set()))
            subfolders = sorted(context.user_data.get('quiz_subjmulti_subfolders_selected', set()))
            if not subjects or not folders:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                ctx_token = self.register_subjmulti_full_ctx(subjects, folders, subfolders or None)
                text, keyboard = self.build_subjmulti_full_count_menu(subjects, folders, subfolders or None, ctx_token, "qzsmsf_show")
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        # ==========================================================
        # NEW: Multi-SUBJECT → Multi-CHAPTER → Multi-SUB-FOLDER → count/timer/start (step 4)
        # ==========================================================
        elif data.startswith("qzsmf_backcnt_"):
            token = data[len("qzsmf_backcnt_"):]
            combo = self.resolve_subjmulti_full_ctx(token)
            if not combo:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subjects, folders, subfolders = combo
                had_sf = context.user_data.get('quiz_subjmulti_had_subfolders', bool(subfolders))
                back_cb = "qzsmsf_show" if had_sf else "qzsmch_show"
                text, keyboard = self.build_subjmulti_full_count_menu(subjects, folders, subfolders, token, back_cb)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzsmf_cntcustom_"):
            token = data[len("qzsmf_cntcustom_"):]
            combo = self.resolve_subjmulti_full_ctx(token)
            if not combo:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subjects, folders, subfolders = combo
                query_filter = {'subject': {'$in': list(subjects)}, 'is_active': True}
                if folders:
                    query_filter['folder'] = {'$in': list(folders)}
                if subfolders:
                    query_filter['subfolder'] = {'$in': list(subfolders)}
                total = self.mongo.count_documents('quizzes', query_filter)
                context.user_data['await'] = 'quiz_subjmulti_full_custom_count'
                context.user_data['quiz_subjmulti_full_ctx_token'] = token
                await query.edit_message_text(f"✏️ Send the number of questions you want (1-{total}).")
        elif data.startswith("qzsmf_cnt_"):
            # format: qzsmf_cnt_<ctx_token>_<count>
            body = data[len("qzsmf_cnt_"):]
            ctx_token, _, count_str = body.rpartition('_')
            count = int(count_str)
            text, keyboard = self.build_subjmulti_full_timer_menu(ctx_token, count)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzsmf_tmrcustom_"):
            # format: qzsmf_tmrcustom_<ctx_token>_<count>
            body = data[len("qzsmf_tmrcustom_"):]
            ctx_token, _, count_str = body.rpartition('_')
            context.user_data['await'] = 'quiz_subjmulti_full_custom_timer'
            context.user_data['quiz_subjmulti_full_ctx_token'] = ctx_token
            context.user_data['quiz_subjmulti_full_setup_count'] = int(count_str)
            await query.edit_message_text("✏️ Send the time limit per question, in seconds (5-600).")
        elif data.startswith("qzsmf_tmr_"):
            # format: qzsmf_tmr_<ctx_token>_<count>_<secs>
            body = data[len("qzsmf_tmr_"):]
            rest, _, secs_str = body.rpartition('_')
            ctx_token, _, count_str = rest.rpartition('_')
            combo = self.resolve_subjmulti_full_ctx(ctx_token)
            if not combo:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                subjects, folders, subfolders = combo
                await self.launch_quiz_session_subjmulti(
                    context, query.message.chat_id, list(subjects),
                    int(count_str), int(secs_str), edit_query=query,
                    folders=list(folders) if folders else None,
                    subfolders=list(subfolders) if subfolders else None)
        elif data.startswith("qzsc_backcnt_"):
            token = data[len("qzsc_backcnt_"):]
            subjects = self.resolve_subj_multi_ctx(token)
            if not subjects:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                text, keyboard = self.build_subjmulti_count_menu(list(subjects), token)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzsc_cntcustom_"):
            token = data[len("qzsc_cntcustom_"):]
            subjects = self.resolve_subj_multi_ctx(token)
            if not subjects:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                query_filter = {'subject': {'$in': list(subjects)}, 'is_active': True}
                total = self.mongo.count_documents('quizzes', query_filter)
                context.user_data['await'] = 'quiz_subjmulti_custom_count'
                context.user_data['quiz_subjmulti_ctx_token'] = token
                await query.edit_message_text(f"✏️ Send the number of questions you want (1-{total}).")
        elif data.startswith("qzsc_cnt_"):
            # format: qzsc_cnt_<ctx_token>_<count>
            body = data[len("qzsc_cnt_"):]
            ctx_token, _, count_str = body.rpartition('_')
            count = int(count_str)
            text, keyboard = self.build_subjmulti_timer_menu(ctx_token, count)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("qzst_tmrcustom_"):
            # format: qzst_tmrcustom_<ctx_token>_<count>
            body = data[len("qzst_tmrcustom_"):]
            ctx_token, _, count_str = body.rpartition('_')
            context.user_data['await'] = 'quiz_subjmulti_custom_timer'
            context.user_data['quiz_subjmulti_ctx_token'] = ctx_token
            context.user_data['quiz_subjmulti_setup_count'] = int(count_str)
            await query.edit_message_text("✏️ Send the time limit per question, in seconds (5-600).")
        elif data.startswith("qzst_tmr_"):
            # format: qzst_tmr_<ctx_token>_<count>_<secs>
            body = data[len("qzst_tmr_"):]
            rest, _, secs_str = body.rpartition('_')
            ctx_token, _, count_str = rest.rpartition('_')
            subjects = self.resolve_subj_multi_ctx(ctx_token)
            if not subjects:
                text, keyboard = self.build_user_subject_menu()
                await query.edit_message_text(
                    "⚠️ Session expired — browse again below.\n\n" + text,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await self.launch_quiz_session_subjmulti(
                    context, query.message.chat_id, list(subjects),
                    int(count_str), int(secs_str), edit_query=query)
    
    async def remove_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Remove a group from the list"""
        self.mongo.delete_one('groups', {'chat_id': chat_id})
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ Group removed from database.\n\n"
            f"The bot will stop sending quizzes to this group."
        )
    
    async def show_group_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Show statistics for a specific group"""
        group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        if not group:
            await update.callback_query.answer("Group not found!")
            return
        
        status = "🟢 Active" if group.get('is_active', True) else "🔴 Inactive"
        
        stats_text = (
            f"📊 **Group Statistics**\n\n"
            f"🏷️ **Name:** {group['title']}\n"
            f"🆔 **ID:** {group['chat_id']}\n"
            f"📅 **Added:** {datetime.fromisoformat(group['added_date']).strftime('%Y-%m-%d')}\n"
            f"📤 **Auto Quizzes Received:** {group.get('quizzes_received', 0)}\n"
            f"🎯 **Manual Quizzes Received:** {group.get('manual_quizzes_received', 0)}\n"
            f"👥 **Members:** {group.get('member_count', 'Unknown')}\n"
            f"🕐 **Last Activity:** {datetime.fromisoformat(group['last_activity']).strftime('%Y-%m-%d %H:%M')}\n"
            f"📊 **Status:** {status}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("🚫 Remove Group", callback_data=f"remove_group_{chat_id}")],
            [InlineKeyboardButton("👥 All Groups", callback_data="manage_groups")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors in the bot"""
        try:
            raise context.error
        except Exception as e:
            print(f"⚠️ Bot error: {type(e).__name__}: {e}")
            
            # Log the full error for debugging
            import traceback
            traceback.print_exc()
            
            # If it's a parsing error, try to send a simplified message
            if "Can't parse entities" in str(e):
                print("⚠️ Markdown/HTML parsing error detected")
                # Try to send a fallback message to admin if this was a report
                try:
                    if update and update.effective_chat and update.effective_chat.id == ADMIN_USER_ID:
                        await context.bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text="⚠️ A quiz report failed to send due to formatting issues. Please check the bot logs."
                        )
                except:
                    pass
        return
    
    def setup_handlers(self):
        """Setup bot handlers"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("broadcast", self.start_broadcast))
        self.application.add_handler(CommandHandler("export", self.export_data))
        self.application.add_handler(CommandHandler("groups", self.manage_groups))
        self.application.add_handler(CommandHandler("setdelay", self.set_quiz_interval_command))
        self.application.add_handler(CommandHandler("setexplanation", self.set_explanation_command))
        self.application.add_handler(CommandHandler("rquiz", self.send_immediate_quiz))
        self.application.add_handler(CommandHandler("reset", self.reset_quizzes_command))
        self.application.add_handler(CommandHandler("qreport", self.report_quiz_command))
        self.application.add_handler(CommandHandler("view", self.view_report_command))
        
        # Add new group list commands
        self.application.add_handler(CommandHandler("grouplist", self.list_groups_with_links))
        self.application.add_handler(CommandHandler("groupslist", self.quick_groups_list))  # Alternative command
        self.application.add_handler(CommandHandler("grouplinks", self.export_group_links))
        
        # NEW: sudo management commands
        self.application.add_handler(CommandHandler("addsudo", self.add_sudo_command))
        self.application.add_handler(CommandHandler("remsudo", self.remove_sudo_command))
        
        # NEW: /quiz (any user, private chat only), /done (admin exits quiz-saving
        # mode) and /stop (user ends their running quiz and sees the result)
        self.application.add_handler(CommandHandler("quiz", self.quiz_command))
        self.application.add_handler(CommandHandler("done", self.done_adding_command))
        self.application.add_handler(CommandHandler("stop", self.stop_quiz_command))
        
        # NEW: grade answers + AUTO-SEND the next question when the user votes
        self.application.add_handler(PollAnswerHandler(self.handle_poll_answer))
        
        # Handle both text messages and polls.
        # NOTE: The old second registration of handle_interval_input was dead code
        # (PTB only runs the FIRST matching handler in a group) — removed.
        # handle_private_message internally routes explanation/interval input,
        # the new await-states and poll saving.
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.POLL) & ~filters.COMMAND, 
            self.handle_private_message
        ))
        
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        # Add error handler
        self.application.add_error_handler(self.error_handler)
    
    async def start_scheduler(self):
        """Start the quiz scheduler"""
        while True:
            await asyncio.sleep(self.quiz_interval)  # Use configurable interval
            await self.send_random_quiz()
    
    async def run_bot(self):
        """Run the bot"""
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        
        # Start the scheduler
        asyncio.create_task(self.start_scheduler())
        
        print("🤖 Bot is starting...")
        await self.application.initialize()
        await self.application.start()
        # FIX: clear any leftover webhook + pending updates before polling.
        # Prevents "Conflict: terminated by other getUpdates request" when a
        # webhook was set previously, or a stale connection is still open.
        try:
            await self.application.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            print(f"⚠️ delete_webhook skipped: {e}")
        await self.application.updater.start_polling(drop_pending_updates=True)
        
        quiz_interval_hours = self.quiz_interval / 3600
        
        # Filter out private chats for stats
        real_groups = [g for g in self.groups if g['chat_id'] < 0]
        
        print(f"✅ Bot is now running with MongoDB support!")
        print(f"⏰ Quiz interval: {quiz_interval_hours} hours")
        print(f"📊 Loaded {len(self.quizzes)} quizzes and {len(real_groups)} groups from database")
        print(f"🎯 /rquiz command enabled for group admins")
        print(f"🔄 /reset command available for admin")
        print(f"👥 NEW: /grouplist command for detailed group list with invite links")
        print(f"👥 NEW: /groupslist command for quick group overview")
        print(f"👥 NEW: /grouplinks command for links export")
        print(f"⚠️ NEW: /qreport command for users to report quizzes")
        print(f"🔍 NEW: /view <report_id> command to view specific reports")
        print(f"🔄 IMPROVED Anti-repeat system active: Tracks last {self.max_recent_track} sent quizzes")
        print(f"👤 Quiz acceptance: Both anonymous and non-anonymous QUIZ MODE polls accepted")
        print(f"📤 Quiz sending: ALWAYS sends as NON-ANONYMOUS (voters visible)")
        print(f"👮 Quiz moderation system active - reports go to admin DM")
        print(f"🔒 Security: Bot will NOT send quizzes to admin's private chat")
        print(f"🛡️ Error handler installed to catch parsing errors")
        print(f"🗑️ Report confirmations auto-delete after 10 seconds to avoid message clutter")
        print(f"📨 Reports to admin now include Report ID for easy reference")
        print(f"👑 Sudo users: {len(self.sudo_users)} additional admins")
        print(f"🗂 NEW: Hierarchical quiz storage (Subject → Quiz Folder → Questions)")
        print(f"🗂 NEW: 'Manage Quiz Folders' in dashboard (view/create/rename/delete)")
        print(f"📝 NEW: Admin quiz-saving flow: Subject → Folder → send polls → /done")
        print(f"🎮 NEW: /quiz command for all users (private chat) with per-user sessions")
        print(f"⚡ NEW: Auto-next — the next question is sent automatically after each answer")
        print(f"🛑 NEW: /stop command ends a running quiz and shows the result with score")
        print(f"🎯 NEW: /rquiz [Subject] [Quiz Folder] optional filters")
        print(f"📦 Old quizzes migrated to: General → Uncategorized")
        
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)

def run_flask():
    """Run Flask app"""
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return "Quiz Poll Bot is running with MongoDB!"
    
    @app.route('/health')
    def health():
        return "OK", 200
    
    print(f"🌐 Flask server starting on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def run_bot():
    """Run the bot in its own thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    global bot_instance
    bot_instance = QuizBot()
    
    try:
        loop.run_until_complete(bot_instance.run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Bot error: {e}")
    finally:
        loop.close()

def main():
    """Main function to start both services"""
    # Start Flask in main thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start bot in current thread (this will block)
    run_bot()

if __name__ == '__main__':
    main()
