import os
import json
import random
import asyncio
import csv
import threading
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
PORT = int(os.getenv('PORT', 10000))
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/quizbot')

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

class QuizBot:
    def __init__(self):
        self.application = None
        self.mongo = MongoDB(MONGODB_URI)
        self.quizzes = self.load_quizzes()
        self.groups = self.load_groups()
        self.settings = self.load_settings()
        self.stats = self.load_stats()
        self.broadcast_mode = {}
        self.scheduler_task = None
        self.quiz_interval = self.settings.get('quiz_interval', 3600)  # Default 1 hour
        self.recently_sent_quizzes = []  # Track recently sent quiz IDs
        self.max_recent_track = 10  # Keep track of last 10 sent quizzes
        
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

    def get_random_quiz(self, exclude_recent_count=8):
        """Get a random quiz that hasn't been sent recently - IMPROVED ANTI-REPEAT"""
        if not self.quizzes:
            return None
        
        # Get active quizzes only
        active_quizzes = [q for q in self.quizzes if q.get('is_active', True)]
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
            if user_id == ADMIN_USER_ID:
                keyboard = [
                    [InlineKeyboardButton("📊 View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")],
                    [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
                    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
                    [InlineKeyboardButton("👥 Manage Groups", callback_data="manage_groups")],
                    [InlineKeyboardButton("📋 Export Data", callback_data="export_data")],
                    [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
                    [InlineKeyboardButton("⚠️ View Reports", callback_data="view_reports")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                quiz_interval_hours = self.quiz_interval / 3600
                
                await update.message.reply_text(
                    f"👋 **Admin Dashboard**\n\n"
                    f"I'm your Quiz Bot! Choose an option below:\n\n"
                    f"📊 **Statistics** - View detailed bot analytics\n"
                    f"📝 **Add Quiz** - Create and send me a QUIZ MODE poll to save as quiz\n"
                    f"⚙️ **Settings** - Configure bot settings (Current: {quiz_interval_hours}h interval)\n"
                    f"📢 **Broadcast** - Send message to all groups\n"
                    f"👥 **Manage Groups** - View and manage groups\n"
                    f"📋 **Export Data** - Export quizzes and stats\n"
                    f"🔄 **Reset Quizzes** - Delete all saved quizzes\n"
                    f"⚠️ **View Reports** - Check reported quizzes\n\n"
                    f"To add a quiz: Create a QUIZ MODE poll and send it to me!",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "👋 Hello! I'm a quiz bot that sends random quiz polls regularly.\n\n"
                    "Add me to your group and make me an admin to start receiving fun quiz polls!\n\n"
                    "⚡ **Group Commands:**\n"
                    "• /rquiz - Send immediate random quiz (Group admins only)\n"
                    "• /qreport - Report a quiz for review (Reply to a quiz with this command)"
                )
        else:
            # Bot added to a group
            await self.add_to_group(update)
    
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
        if update.effective_user.id == ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        # Check if it's a poll
        if update.message.poll:
            await self.save_poll_quiz(update, update.message.poll)
        else:
            await update.message.reply_text(
                "❌ Please send a QUIZ MODE poll to save as a quiz!\n\n"
                "To create a QUIZ MODE poll:\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. ✅ Enable 'Quiz Mode' and set the correct answer\n"
                "5. Send it to me\n\n"
                "I'll automatically save it as a quiz!\n\n"
                "📝 Note: Only QUIZ MODE polls are accepted (with correct answers)\n"
                "👤 Note: I accept both anonymous and non-anonymous QUIZ MODE polls, but will always send as NON-ANONYMOUS"
            )
    
    async def save_poll_quiz(self, update: Update, poll):
        """Save a poll as a quiz - BOTH ANONYMOUS AND NON-ANONYMOUS QUIZ MODE POLLS ARE ACCEPTED"""
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
        
        # Format options for display
        options_text = "\n".join([f"• {option}" for option in quiz['options']])
        correct_answer = quiz['options'][quiz['correct_option_id']]
        anonymous_status = "Anonymous" if quiz['is_anonymous'] else "Non-anonymous"
        
        await update.message.reply_text(
            f"✅ **Quiz Saved Successfully!**\n\n"
            f"📝 **Question:** {quiz['question']}\n\n"
            f"📋 **Options:**\n{options_text}\n\n"
            f"✅ **Correct Answer:** {correct_answer}\n"
            f"👤 **Original Setting:** {anonymous_status}\n"
            f"📊 Total quizzes: {len(self.quizzes)}\n"
            f"👥 Will be sent to: {len(self.groups)} groups\n"
            f"⏰ Next quiz in: {self.quiz_interval / 3600} hours\n\n"
            f"💡 Note: When sent to groups, quizzes will always be NON-ANONYMOUS (voters visible)\n"
            f"💡 Group admins can use /rquiz to send immediate quizzes!\n"
            f"⚠️ Users can report quizzes with /qreport command"
        )
    
    async def send_random_quiz(self):
        """Send a random quiz poll to all groups"""
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
                open_period=0,  # No time limit
            )
        
        # Update group stats
        group['quizzes_received'] = group.get('quizzes_received', 0) + 1
        group['last_activity'] = datetime.now().isoformat()
        self.save_group(group)
        
        # Track engagement
        if str(group['chat_id']) not in self.stats['group_engagement']:
            self.stats['group_engagement'][str(group['chat_id'])] = 0
        self.stats['group_engagement'][str(group['chat_id'])] += 1
    
    async def send_immediate_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rquiz command - send immediate random quiz to current group"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        chat_title = update.effective_chat.title
        
        # Check if it's a group chat
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command can only be used in groups!")
            return
        
        # Check if user is admin of the group or bot admin
        is_admin = False
        
        # Check if user is bot admin
        if user_id == ADMIN_USER_ID:
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
            quiz = self.get_random_quiz(exclude_recent_count=5)  # Slightly less strict for manual sends
            
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
    
    async def report_quiz_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /qreport command - report a quiz for review"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        message_id = update.effective_message.message_id
        
        # Check if it's a group chat
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command can only be used in groups!")
            return
        
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
        
        # Extract quiz information
        quiz_info = {
            'chat_id': chat_id,
            'message_id': update.message.reply_to_message.message_id,
            'question': replied_poll.question,
            'options': [option.text for option in replied_poll.options],
            'correct_option_id': replied_poll.correct_option_id,
            'reported_by': {
                'user_id': user_id,
                'username': update.effective_user.username,
                'first_name': update.effective_user.first_name,
            },
            'report_time': datetime.now().isoformat(),
            'group_name': update.effective_chat.title,
            'original_message_link': f"https://t.me/c/{str(chat_id)[4:]}/{update.message.reply_to_message.message_id}"
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
        
        # Send confirmation to the user
        await update.message.reply_text(
            f"✅ **Quiz Reported Successfully!**\n\n"
            f"📝 **Question:** {replied_poll.question[:100]}...\n\n"
            f"The quiz has been forwarded to the admin for review.\n"
            f"Thank you for helping improve the quiz quality!"
        )
        
        # Forward the quiz to admin with action buttons
        await self.send_quiz_report_to_admin(context, quiz_info, report_id)
    
    async def send_quiz_report_to_admin(self, context: ContextTypes.DEFAULT_TYPE, quiz_info: dict, report_id: str):
        """Send quiz report to admin with action buttons"""
        
        # Format quiz information
        options_text = "\n".join([f"• {option}" for option in quiz_info['options']])
        correct_answer = quiz_info['options'][quiz_info['correct_option_id']]
        
        # Handle username display
        username = quiz_info['reported_by']['username']
        username_display = f" (@{username})" if username else ""
        
        report_text = (
            f"⚠️ **QUIZ REPORTED FOR REVIEW**\n\n"
            f"📝 **Question:** {quiz_info['question']}\n\n"
            f"📋 **Options:**\n{options_text}\n\n"
            f"✅ **Correct Answer:** {correct_answer}\n\n"
            f"📊 **Report Details:**\n"
            f"• 👤 Reported by: {quiz_info['reported_by']['first_name']}{username_display}\n"
            f"• 👥 Group: {quiz_info['group_name']}\n"
            f"• 🕐 Time: {datetime.fromisoformat(quiz_info['report_time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 🔗 Message: [View Original]({quiz_info['original_message_link']})\n\n"
            f"**What would you like to do with this quiz?**"
        )
        
        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"delete_quiz_{report_id}"),
                InlineKeyboardButton("👁️ Ignore Report", callback_data=f"ignore_report_{report_id}")
            ],
            [
                InlineKeyboardButton("📝 View Similar Quizzes", callback_data=f"view_similar_{report_id}"),
                InlineKeyboardButton("📊 View All Reports", callback_data="view_reports")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send to admin
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=report_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
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
            
            for i, report in enumerate(pending_reports[:5], 1):  # Show only first 5
                report_time = datetime.fromisoformat(report['report_time']).strftime('%m/%d %H:%M')
                response_text += (
                    f"{i}. **{report['question'][:60]}...**\n"
                    f"   👤 {report['reported_by']['first_name']} | "
                    f"👥 {report['group_name']}\n"
                    f"   🕐 {report_time} | "
                    f"[View]({report['original_message_link']})\n"
                    f"   [Review](callback:report_{report['_id']})\n\n"
                )
            
            if len(pending_reports) > 5:
                response_text += f"... and {len(pending_reports) - 5} more pending reports\n\n"
            
            response_text += f"📈 **Statistics:**\n"
            response_text += f"• Total reports: {len(total_reports)}\n"
            response_text += f"• Pending: {len(pending_reports)}\n"
            response_text += f"• Resolved: {len(total_reports) - len(pending_reports)}\n"
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="view_reports")],
                [InlineKeyboardButton("🗑️ Clear All Resolved", callback_data="clear_resolved_reports")],
                [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
                [InlineKeyboardButton("✅ Close", callback_data="close_report")]
            ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(response_text, reply_markup=reply_markup)
    
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
        
        # Recreate the original report message
        options_text = "\n".join([f"• {option}" for option in report['options']])
        correct_answer = report['options'][report['correct_option_id']]
        
        # Handle username display
        username = report['reported_by']['username']
        username_display = f" (@{username})" if username else ""
        
        report_text = (
            f"⚠️ **QUIZ REPORTED FOR REVIEW**\n\n"
            f"📝 **Question:** {report['question']}\n\n"
            f"📋 **Options:**\n{options_text}\n\n"
            f"✅ **Correct Answer:** {correct_answer}\n\n"
            f"📊 **Report Details:**\n"
            f"• 👤 Reported by: {report['reported_by']['first_name']}{username_display}\n"
            f"• 👥 Group: {report['group_name']}\n"
            f"• 🕐 Time: {datetime.fromisoformat(report['report_time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 🔗 Message: [View Original]({report['original_message_link']})\n\n"
            f"**What would you like to do with this quiz?**"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"delete_quiz_{report_id}"),
                InlineKeyboardButton("👁️ Ignore Report", callback_data=f"ignore_report_{report_id}")
            ],
            [
                InlineKeyboardButton("📝 View Similar Quizzes", callback_data=f"view_similar_{report_id}"),
                InlineKeyboardButton("📊 View All Reports", callback_data="view_reports")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(report_text, reply_markup=reply_markup)
    
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID or not context.user_data.get('waiting_for_explanation'):
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
        
        if user_id != ADMIN_USER_ID:
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
            f"   • Quizzes added: {quizzes_added}\n"
            f"   • Most sent quiz: {most_sent['sent_count'] if most_sent else 0} times\n"
            f"   • Quizzes deleted by reports: {quizzes_deleted_by_reports}\n\n"
            
            f"👥 **Groups Analytics**\n"
            f"   • Total groups: {total_groups}\n"
            f"   • Active groups: {active_groups_count}\n"
            f"   • Recently active: {recently_active}\n"
            f"   • Total quizzes sent: {total_quizzes_sent}\n"
            f"   • Manual quizzes sent: {manual_quizzes_sent}\n\n"
            
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
        
        if user_id != ADMIN_USER_ID:
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
            f"👥 **Active Groups**: {len([g for g in self.groups if g.get('is_active', True)])}\n"
            f"📝 **Active Quizzes**: {len([q for q in self.quizzes if q.get('is_active', True)])}\n"
            f"🎯 **Manual Quizzes Sent**: {self.stats.get('manual_quizzes_sent', 0)}\n"
            f"⚠️ **Quiz Reports**: {self.stats.get('quiz_reports_received', 0)}\n\n"
            f"💡 Use /setdelay <time> to change the quiz interval\n"
            f"💡 Use /setexplanation to change quiz explanation\n"
            f"💡 Group admins can use /rquiz for immediate quizzes\n"
            f"⚠️ Users can report quizzes with /qreport"
        )
        
        keyboard = [
            [InlineKeyboardButton("🕐 Set Quiz Interval", callback_data="set_interval")],
            [InlineKeyboardButton("📝 Set Explanation", callback_data="set_explanation")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("🔄 Refresh Groups", callback_data="refresh_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID or not context.user_data.get('waiting_for_interval'):
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        try:
            # Export quizzes to CSV
            if self.quizzes:
                with open('quizzes_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['_id', 'type', 'question', 'options', 'is_anonymous', 'allows_multiple_answers', 'correct_option_id', 'added_date', 'sent_count', 'manual_sent_count', 'last_sent', 'is_active']
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
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
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Reload groups from MongoDB
        self.groups = self.load_groups()
        
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        
        await update.callback_query.answer(f"Groups refreshed! {active_groups} active groups loaded.")
    
    async def list_groups_with_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /grouplist command - list all groups with invite links"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        if not self.groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        inactive_groups = [g for g in self.groups if not g.get('is_active', True)]
        
        # Show loading message
        loading_msg = await update.message.reply_text("🔄 Fetching group links... This may take a moment.")
        
        groups_text = f"👥 **Groups List ({len(self.groups)} total)**\n\n"
        groups_text += f"🟢 Active: {len(active_groups)}\n"
        groups_text += f"🔴 Inactive: {len(inactive_groups)}\n\n"
        
        all_links_text = "📋 **Group List with Links**\n\n"
        failed_groups = []
        success_count = 0
        
        # Process groups in batches to avoid rate limiting
        for i, group in enumerate(self.groups, 1):
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
            f"✅ Successfully fetched links: {success_count}/{len(self.groups)}\n"
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
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        if not self.groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        inactive_groups = [g for g in self.groups if not g.get('is_active', True)]
        
        groups_text = f"👥 **Groups Summary ({len(self.groups)} total)**\n\n"
        
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
            f"• Active groups percentage: {(len(active_groups)/len(self.groups)*100 if self.groups else 0):.1f}%\n\n"
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
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        if not self.groups:
            await update.message.reply_text("❌ No groups found in database.")
            return
        
        loading_msg = await update.message.reply_text("🔄 Generating group links...")
        
        links_text = "🔗 **Group Invite Links**\n\n"
        links_only = "📋 **Links Only (for export):**\n\n"
        
        success_count = 0
        
        for group in self.groups:
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
            f"📊 Generated {success_count} links from {len(self.groups)} groups\n"
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
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "stats":
            await self.show_stats(update, context)
        elif data == "add_quiz":
            await query.edit_message_text(
                "📝 **Add New Quiz Poll**\n\n"
                "To add a quiz:\n\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. ✅ **Enable 'Quiz Mode' and set the correct answer**\n"
                "5. Send the poll to me\n\n"
                "📢 **Important:** I only accept QUIZ MODE polls (with correct answers)\n"
                "📢 **Important:** I accept both anonymous and non-anonymous QUIZ MODE polls\n"
                "📢 **Important:** When sent to groups, quizzes will ALWAYS be NON-ANONYMOUS (voters visible)\n\n"
                "I'll automatically save it and send it to groups!\n\n"
                "💡 Group admins can use /rquiz for immediate quizzes\n"
                "⚠️ Users can report quizzes with /qreport"
            )
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
        
        # Add new group list commands
        self.application.add_handler(CommandHandler("grouplist", self.list_groups_with_links))
        self.application.add_handler(CommandHandler("groupslist", self.quick_groups_list))  # Alternative command
        self.application.add_handler(CommandHandler("grouplinks", self.export_group_links))
        
        # Handle both text messages and polls
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.POLL) & ~filters.COMMAND, 
            self.handle_private_message
        ))
        
        # Handle interval input from settings menu
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            self.handle_interval_input
        ))
        
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
    
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
        await self.application.updater.start_polling()
        
        quiz_interval_hours = self.quiz_interval / 3600
        print(f"✅ Bot is now running with MongoDB support!")
        print(f"⏰ Quiz interval: {quiz_interval_hours} hours")
        print(f"📊 Loaded {len(self.quizzes)} quizzes and {len(self.groups)} groups from database")
        print(f"🎯 /rquiz command enabled for group admins")
        print(f"🔄 /reset command available for admin")
        print(f"👥 NEW: /grouplist command for detailed group list with invite links")
        print(f"👥 NEW: /groupslist command for quick group overview")
        print(f"👥 NEW: /grouplinks command for links export")
        print(f"⚠️ NEW: /qreport command for users to report quizzes")
        print(f"🔄 IMPROVED Anti-repeat system active: Tracks last {self.max_recent_track} sent quizzes")
        print(f"👤 Quiz acceptance: Both anonymous and non-anonymous QUIZ MODE polls accepted")
        print(f"📤 Quiz sending: ALWAYS sends as NON-ANONYMOUS (voters visible)")
        print(f"👮 Quiz moderation system active - reports go to admin DM")
        
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