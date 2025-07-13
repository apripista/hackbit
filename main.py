import logging
import os
from logging.handlers import RotatingFileHandler
import math
import random
import re
import smtplib
import string
import uuid
from functools import wraps
from html import escape
from math import ceil
import requests
from flask import Flask, render_template, request, session, flash, redirect, url_for, g, current_app, jsonify, send_file
from flask_mail import Mail, Message
from psycopg2 import ProgrammingError, OperationalError, DataError, IntegrityError
from requests import RequestException
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_moment import Moment
from celery import Celery
import psycopg2
from dotenv import load_dotenv
import secrets
import bleach
from datetime import date, datetime, timedelta, timezone
from flask import send_from_directory
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import namedtuple
from error_handlers import register_error_handlers

logging.getLogger('celery.utils.functional').setLevel(logging.INFO)

# Load environment variables
load_dotenv()
# Validate required environment variables (database and email)
required_env_vars = [
    "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
    "MAIL_SERVER", "MAIL_PORT", "MAIL_USE_TLS", "MAIL_USE_SSL",
    "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER"
]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Configure logging
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logger = logging.getLogger('insipirahub')
env = os.getenv('FLASK_ENV', 'development')
log_level = logging.DEBUG if env == 'development' else logging.INFO
logger.setLevel(log_level)

# Suppress smtplib and Celery warnings
logging.getLogger('smtplib').setLevel(logging.ERROR)  # Only log smtplib errors
logging.getLogger('celery').setLevel(logging.INFO)  # Celery logs at INFO
logging.getLogger('celery.worker').setLevel(logging.INFO)  # Worker logs at INFO
logging.getLogger('celery.utils.functional').setLevel(logging.INFO)  # Already in your code, kept for consistency

file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'app.log'),
    maxBytes= 10 * 1024 * 1024,  # 10MB
    backupCount=5
)   

file_handler.setLevel(log_level)
file_formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] [%(module)s:%(lineno)d]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR if env == 'production' else log_level)
console_handler.setFormatter(file_formatter)

logger.addHandler(file_handler)
if env != 'production':
    logger.addHandler(console_handler)

# Initialize Flask app
app = Flask(__name__, static_folder="Uploads", static_url_path="/Uploads")
app.logger = logger
app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.config["UPLOAD_FOLDER"] = "Uploads"
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1MB limit

# Load SMTP settings from .env
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT"))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS").lower() == "true"
app.config["MAIL_USE_SSL"] = os.getenv("MAIL_USE_SSL").lower() == "true"
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER")
app.config["MAIL_DEBUG"] = False  # Disable SMTP debug logs

# Initialize Flask-Mail, Moment, error_handlers
mail = Mail(app)
moment = Moment(app)
register_error_handlers(app)

# Celery configuration
app.config.update(
    broker_url=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    result_backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0'),
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=5,
    broker_connection_timeout=10
)

# Initialize Celery
celery = Celery(app.name, broker=app.config['broker_url'])
celery.conf.update(app.config)
logger.debug(f"Celery broker: {app.config['broker_url']}, result_backend: {app.config['result_backend']}")

celery.conf.update(
    include=['main']  # Only load tasks from main.py
)

# Configure Celery logger to use the same handlers as Flask app
celery_logger = logging.getLogger('celery')
celery_logger.setLevel(log_level)
celery_logger.addHandler(file_handler)
if env != 'production':
    celery_logger.addHandler(console_handler)

# Constants
MIN_LENGTH = 2
MAX_LENGTH = 100
MAX_EMAIL_LENGTH = 255

UPLOAD_FOLDER = "Uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

NAME_REGEX = r'^[a-zA-Z]{2,100}$'
EMAIL_REGEX = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'

# Allowed HTML tags and attributes for sanitization
ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS | {'p', 'div', 'h1', 'h2', 'strong', 'em', 'br', 'table', 'tr', 'td'}
ALLOWED_ATTRIBUTES = {'a': ['href'], 'table': ['style'], 'td': ['style'], 'div': ['style']}

# Session configuration
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=15)  # session lifetime to 15 minutes
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"  # secure cookies in production
app.config["SESSION_COOKIE_HTTPONLY"] = True  # Prevent JavaScript access to cookies
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF protection



# Database connection functions
def get_db_connection():
    db_connection = getattr(g, "_db_connection", None)
    if db_connection is None:
        db_connection = g._db_connection = psycopg2.connect(
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
    return db_connection

@app.teardown_appcontext
def close_db_connection(exception=None):
    db_connection = getattr(g, "_db_connection", None)
    if db_connection is not None:
        db_connection.close()


@celery.task(bind=True, max_retries=3, rate_limit="100/h")
def process_registration_emails(self, account_id, email, username, first_name, last_name, country, verification_token, security_pin):
    with app.app_context():
        try:
            logger.info(f"Starting background task for user: {email}, account_id: {account_id}")

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username, tags=[], strip=True)
            sanitized_first_name = bleach.clean(first_name.title(), tags=[], strip=True)
            sanitized_last_name = bleach.clean(last_name.title(), tags=[], strip=True)
            sanitized_country = bleach.clean(country.title(), tags=[], strip=True)
            sanitized_verification_token = bleach.clean(verification_token, tags=[], strip=True)
            sanitized_security_pin = bleach.clean(str(security_pin), tags=[], strip=True)

            # Verify account_id exists in accounts table
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
                    if not cursor.fetchone():
                        logger.error(f"Account ID {account_id} does not exist in accounts table for email: {sanitized_email}")
                        return  # Exit task gracefully

                    # Insert verification token into tokens table with UTC timestamps
                    verification_sent_time = datetime.now(timezone.utc)
                    verification_token_expiration = verification_sent_time + timedelta(minutes=10)
                    cursor.execute(
                        "INSERT INTO tokens (account_id, username, email, verification_token, verification_sent_time, verification_token_expiration) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            account_id,
                            sanitized_username,
                            sanitized_email,
                            sanitized_verification_token,
                            verification_sent_time,
                            verification_token_expiration,
                        ),
                    )
                    conn.commit()
                    logger.info(f"Inserted verification token for account_id: {account_id}")

            server_address = "http://localhost:5000"  # Update to https://inspirahub.com when domain is ready
            support_email = "intuitivers@gmail.com"
            sender_email = "intuitivers@gmail.com"

            # Send Verification Email (Immediate)
            verification_link = f"{server_address}/verify/{sanitized_verification_token}"
            verification_subject = "Inspirahub: Verify Your Email"
            verification_plain_body = (
                f"Dear {sanitized_username},\n\n"
                f"Welcome to Inspirahub! Please click the link below to verify your email:\n"
                f"{verification_link}\n\n"
                f"This link expires in 10 minutes. If you didn’t sign up, ignore this email or contact {support_email}.\n\n"
                f"Best regards,\n"
                f"Inspirahub Team\n"
                f"{server_address}"
            )
            verification_html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Inspirahub Email Verification</title>
            </head>
            <body style="font-family: 'Verdana', Arial, sans-serif; color: #333333; background-color: #f5f3ff; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 15px; border: 2px solid #805ad5; overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #805ad5, #6b46c1); color: #ffffff; padding: 30px; text-align: center;">
                        <h1 style="margin: 0; font-size: 32px; font-weight: 700;">Inspirahub</h1>
                        <p style="margin: 10px 0 0; font-size: 18px;">Verify Your Email</p>
                    </div>
                    <div style="padding: 35px;">
                        <p style="font-size: 18px; line-height:Exc1.6; margin: 0 0 20px;">Hello {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            Thank you for joining Inspirahub! Please verify your email address: <strong>{sanitized_email}</strong>.
                        </p>
                        <div style="text-align: center; margin: 25px 0;">
                            <a href="{verification_link}" style="display: inline-block; padding: 15px 30px; background-color: #6b46c1; color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 18px; font-weight: 600;">
                                Verify Email
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            Or copy and paste this link: <a href="{verification_link}" style="color: #6b46c1; text-decoration: none;">{verification_link}</a>
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            This link expires in 10 minutes. If you didn’t sign up, contact us at 
                            <a href="mailto:{support_email}" style="color: #6b46c1; text-decoration: none;">{support_email}</a>.
                            Need help? Visit our <a href="{server_address}/help" style="color: #6b46c1; text-decoration: none;">Help Center</a>.
                        </p>
                    </div>
                    <div style="background-color: #e9d8fd; padding: 20px; text-align: center; font-size: 13px; color: #4a5568;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 8px 0 0;">
                            <a href="{server_address}" style="color: #6b46c1; text-decoration: none;">Inspirahub</a> | 
                            <a href="mailto:{support_email}" style="color: #6b46c1; text-decoration: none;">Contact Support</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            verification_msg = Message(
                verification_subject,
                recipients=[sanitized_email],
                sender=sender_email,
                reply_to=support_email
            )
            verification_msg.body = verification_plain_body
            verification_msg.html = verification_html_body
            mail.send(verification_msg)
            logger.info(f"Sent verification email to: {sanitized_email}")

        except psycopg2.Error as e:
            logger.error(f"Database error in process_registration_emails for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60)
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_registration_emails for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60)
        except Exception as e:
            logger.error(f"Unexpected error in process_registration_emails for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60)


@celery.task(bind=True, max_retries=3, rate_limit="100/h")
def send_security_pin_email(self, account_id, email, username, security_pin):
    with app.app_context():
        try:
            logger.info(f"Sending security PIN email to: {email}, account_id: {account_id}")

            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_security_pin = bleach.clean(str(security_pin), tags=[], strip=True)

            server_address = "http://localhost:5000"
            support_email = "intuitivers@gmail.com"
            sender_email = "intuitivers@gmail.com"

            pin_subject = "Inspirahub: Your Account Security PIN"
            pin_plain_body = (
                f"Dear {sanitized_username},\n\n"
                f"Your security PIN for account deletion is: {sanitized_security_pin}\n\n"
                f"Keep this PIN secure, as it’s required to delete your account. If you lose it, contact {support_email}.\n\n"
                f"Best regards,\n"
                f"Inspirahub Team\n"
                f"{server_address}"
            )
            pin_html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Inspirahub Security PIN</title>
            </head>
            <body style="font-family: 'Verdana', Arial, sans-serif; color: #333333; background-color: #fff5f5; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 15px; border: 2px solid #c53030; overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #e53e3e, #c53030); color: #ffffff; padding: 30px; text-align: center;">
                        <h1 style="margin: 0; font-size: 32px; font-weight: 700;">Inspirahub</h1>
                        <p style="margin: 10px 0 0; font-size: 18px;">Your Security PIN</p>
                    </div>
                    <div style="padding: 35px;">
                        <p style="font-size: 18px; line-height: 1.6; margin: 0 0 20px;">Hello {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            Below is your security PIN for account deletion:
                        </p>
                        <div style="text-align: center; margin: 25px 0; padding: 20px; background-color: #fef5f5; border: 2px dashed #c53030; border-radius: 10px;">
                            <p style="font-size: 24px; font-weight: 700; color: #c53030; margin: 0; font-family: 'Courier New', monospace;">{sanitized_security_pin}</p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            Keep this PIN safe. If you lose it, contact 
                            <a href="mailto:{support_email}" style="color: #c53030; text-decoration: none;">{support_email}</a>.
                            Learn more at our <a href="{server_address}/help" style="color: #c53030; text-decoration: none;">Help Center</a>.
                        </p>
                    </div>
                    <div style="background-color: #fed7d7; padding: 20px; text-align: center; font-size: 13px; color: #4a5568;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 8px 0 0;">
                            <a href="{server_address}" style="color: #c53030; text-decoration: none;">Inspirahub</a> | 
                            <a href="mailto:{support_email}" style="color: #c53030; text-decoration: none;">Contact Support</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            pin_msg = Message(
                pin_subject,
                recipients=[sanitized_email],
                sender=sender_email,
                reply_to=support_email
            )
            pin_msg.body = pin_plain_body
            pin_msg.html = pin_html_body
            mail.send(pin_msg)
            logger.info(f"Sent security PIN email to: {sanitized_email}")

        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_security_pin_email for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except Exception as e:
            logger.error(f"Unexpected error in send_security_pin_email for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)

@celery.task(bind=True, max_retries=3, rate_limit="100/h")
def send_welcome_email(self, account_id, email, username, country, security_pin, user_verified):
    with app.app_context():
        try:
            logger.info(f"Sending welcome email to: {email}, account_id: {account_id}")

            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_country = bleach.clean(country.title(), tags=[], strip=True)
            sanitized_security_pin = bleach.clean(str(security_pin), tags=[], strip=True)

            server_address = "http://localhost:5000"
            support_email = "intuitivers@gmail.com"
            sender_email = "intuitivers@gmail.com"
            reverify_link = f"{server_address}/request-verification"

            welcome_subject = "Welcome to Inspirahub"
            welcome_plain_body = (
                f"Dear {sanitized_username},\n\n"
                f"Welcome to Inspirahub! We’re excited to have you in our community. Here’s how to get started:\n"
                f"- Your Security PIN: {sanitized_security_pin} (keep this safe for account deletion).\n"
                f"- Update Your Profile: Add a photo and bio to personalize your account.\n"
                f"- Explore the Community: Discover posts and connect with others from {sanitized_country}.\n"
                f"{'' if user_verified else f'- Verify Your Email: Request a new verification link at {reverify_link} if you haven’t verified yet.'}\n\n"
                f"Questions? Visit our Help Center at {server_address}/help or contact {support_email}.\n"
                f"To stop receiving these emails, delete your account at {server_address}/delete.\n\n"
                f"Best regards,\n"
                f"Inspirahub Team\n"
                f"{server_address}"
            )
            welcome_html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Welcome to Inspirahub</title>
            </head>
            <body style="font-family: 'Verdana', Arial, sans-serif; color: #2d3748; background-color: #e6fffa; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 15px; border: 2px solid #38b2ac; overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #38b2ac, #ed64a6); color: #ffffff; padding: 30px; text-align: center;">
                        <h1 style="margin: 0; font-size: 34px; font-weight: bold;">Welcome to Inspirahub</h1>
                        <p style="margin: 10px 0 0; font-size: 18px;">Start Your Journey!</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 18px; line-height: 1.6; margin: 0 0 20px;">Hello {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px;">
                            Thank you for joining Inspirahub! We’re excited to have you in our community.
                        </p>
                        <h2 style="font-size: 22px; color: #38b2ac; margin: 25px 0 15px; text-align: center;">Important Information</h2>
                        <ul style="list-style: none; padding: 0; margin: 0 0 25px;">
                            <li style="font-size: 16px; line-height: 1.6; margin: 10px 0; padding: 15px; background-color: #f0fff4; border-left: 5px solid #ed64a6; border-radius: 8px;">
                                <strong>Your Security PIN:</strong> {sanitized_security_pin} (keep this safe for account deletion).
                            </li>
                            {"<li style='font-size: 16px; line-height: 1.6; margin: 10px 0; padding: 15px; background-color: #f0fff4; border-left: 5px solid #ed64a6; border-radius: 8px;'><strong>Verify Your Email:</strong> Request a new verification link at <a href='{reverify_link}' style='color: #ed64a6; text-decoration: underline;'>Request Verification</a> if you haven’t verified yet.</li>" if not user_verified else ""}
                        </ul>
                        <h2 style="font-size: 22px; color: #38b2ac; margin: 25px 0 15px; text-align: center;">Get Started</h2>
                        <ul style="list-style: none; padding: 0; margin: 0 0 25px;">
                            <li style="font-size: 16px; line-height: 1.6; margin: 10px 0; padding: 15px; background-color: #f0fff4; border-left: 5px solid #ed64a6; border-radius: 8px;">
                                <strong>Update Your Profile:</strong> Add a photo and bio to personalize your account. 
                                <a href="{server_address}/profile" style="color: #ed64a6; text-decoration: underline;">Edit Profile</a>
                            </li>
                            <li style="font-size: 16px; line-height: 1.6; margin: 10px 0; padding: 15px; background-color: #f0fff4; border-left: 5px solid #ed64a6; border-radius: 8px;">
                                <strong>Explore the Community:</strong> Discover posts and connect with others from {sanitized_country}.
                            </li>
                        </ul>
                        <div style="text-align: center; margin: 25px 0;">
                            <a href="{server_address}/profile" style="display: inline-block; padding: 15px 30px; background: linear-gradient(90deg, #38b2ac, #ed64a6); color: #ffffff; text-decoration: none; border-radius: 10px; font-size: 18px; font-weight: bold;">
                                Get Started
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 20px; text-align: center;">
                            Questions? Visit our <a href="{server_address}/help" style="color: #ed64a6; text-decoration: underline;">Help Center</a> or contact 
                            <a href="mailto:{support_email}" style="color: #ed64a6; text-decoration: underline;">{support_email}</a>.
                            To stop receiving these emails, delete your account at <a href="{server_address}/delete" style="color: #ed64a6; text-decoration: underline;">{server_address}/delete</a>.
                        </p>
                    </div>
                    <div style="background-color: #b2f5ea; padding: 20px; text-align: center; font-size: 14px; color: #2d3748;">
                        <p style="margin: 0; font-size: 16px; font-weight: bold;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 10px 0 0;">
                            <a href="{server_address}" style="color: #ed64a6; text-decoration: underline;">Inspirahub</a> | 
                            <a href="mailto:{support_email}" style="color: #ed64a6; text-decoration: underline;">Contact Support</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            welcome_msg = Message(
                welcome_subject,
                recipients=[sanitized_email],
                sender=sender_email,
                reply_to=support_email
            )
            welcome_msg.body = welcome_plain_body
            welcome_msg.html = welcome_html_body
            welcome_msg.extra_headers = {
                "List-Unsubscribe": f"<mailto:{support_email}?subject=unsubscribe>, <{server_address}/unsubscribe>",
                "Precedence": "bulk"
            }
            mail.send(welcome_msg)
            logger.info(f"Sent welcome email to: {sanitized_email}")

        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_welcome_email for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except Exception as e:
            logger.error(f"Unexpected error in send_welcome_email for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)


# Helper task for delayed welcome email
@celery.task(bind=True, max_retries=3)
def send_welcome(self, email, subject, plain_body, html_body, sender_email, reply_to):
    with app.app_context():
        try:
            msg = Message(
                subject,
                recipients=[email],
                sender=sender_email,
                reply_to=reply_to
            )
            msg.body = plain_body
            msg.html = html_body
            msg.extra_headers = {
                "List-Unsubscribe": f"<mailto:{reply_to}?subject=unsubscribe>, <https://inspirahub.com/unsubscribe>",
                "Precedence": "bulk"
            }
            mail.send(msg)
            logger.info(f"Sent delayed welcome email to: {email}")
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_welcome for {email}: {str(e)}")
            self.retry(countdown=60)


# Generate a random secret verification token
def generate_verification_token(length=64):
    characters = string.ascii_letters + string.digits
    return "".join(secret.choice(characters) for _ in range(length))


def generate_security_pin():
    """Generate a random, unique 7-character alphanumeric security PIN."""
    characters = string.ascii_letters + string.digits  # a-z, A-Z, 0-9
    max_attempts = 100

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for _ in range(max_attempts):
                    pin = ''.join(secrets.choice(characters) for _ in range(7))
                    cursor.execute(
                        "SELECT 1 FROM accounts WHERE security_pin = %s AND pin_deleted_at IS NULL",
                        (pin,)
                    )
                    if not cursor.fetchone():
                        logger.debug(f"Generated unique security PIN: {pin}")
                        return pin
                logger.error("Failed to generate unique security PIN after max attempts")
                raise Exception("Could not generate a unique security PIN")
    except psycopg2.Error as e:
        logger.error(f"Database error in generate_security_pin: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in generate_security_pin: {e}", exc_info=True)
        raise


@app.route("/")
def index():
    return render_template("insipirahub/index.html")


@app.route("/about")
def about():
    return render_template("insipirahub/about.html")


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("You need to login first.", "error")
            return redirect(url_for("login"))
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT role FROM accounts WHERE id = %s", (session["user_id"],))
                    result = cursor.fetchone()
                    if result is None:
                        flash("User not found.", "error")
                        return redirect(url_for("login"))
                    user_role = result[0]  # Access the first (and only) column
                    if user_role == "admin":
                        return f(*args, **kwargs)
                    else:
                        flash("Access denied. You do not have permission to view this page.", "error")
                        return redirect(url_for("view_posts"))
        except psycopg2.Error as e:
            logger.error(f"Database error in admin_required: {e}", exc_info=True)
            flash("A database error occurred. Please try again.", "error")
            return redirect(url_for("view_posts"))
    return decorated_function


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"]
        first_name = request.form["first_name"]
        last_name = request.form["last_name"]
        username = request.form["username"]
        password = request.form["password"]
        country = request.form["country"]
        recaptcha_response = request.form.get("g-recaptcha-response")

        logger.info(f"Registration attempt for email: {email}, username: {username}")

        # Validate reCAPTCHA
        RECAPTCHA_ENABLED = True
        recaptcha_secret = "6LejiBsrAAAAAMmgk3eyGV7fd6952QXGxxanUq5U"
        if RECAPTCHA_ENABLED:
            try:
                recaptcha_data = {
                    "secret": recaptcha_secret,
                    "response": recaptcha_response
                }
                logger.debug("Sending reCAPTCHA verification request")
                recaptcha_verify = requests.post("https://www.google.com/recaptcha/api/siteverify", data=recaptcha_data).json()
                logger.debug(f"reCAPTCHA response: {recaptcha_verify}")
                if not recaptcha_verify.get("success"):
                    logger.debug(f"reCAPTCHA validation failed for username: {username}")
                    flash("reCAPTCHA validation failed. Please check the reCAPTCHA box and try again.", "error")
                    return redirect(url_for("register"))
            except Exception as e:
                logger.error(f"reCAPTCHA validation error: {e}", exc_info=True)
                flash("Error validating reCAPTCHA. Please try again.", "error")
                return redirect(url_for("register"))

        # Validate email format
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            logger.warning(f"Invalid email format: {email}")
            flash("Please provide a valid email address (e.g., user@domain.com).", "error")
            return redirect(url_for("register"))
        if len(email) > MAX_EMAIL_LENGTH:
            logger.warning(f"Email exceeds max length: {len(email)} characters")
            flash(f"Email must be less than {MAX_EMAIL_LENGTH} characters.", "error")
            return redirect(url_for("register"))

        # Validate password strength
        if len(password) < 12:
            logger.warning(f"Password too short for username: {username}")
            flash("Password must be at least 12 characters long.", "error")
            return redirect(url_for("register"))
        if not re.search(r"[A-Z]", password):
            logger.warning(f"Password lacks uppercase letter for username: {username}")
            flash("Password must contain at least one uppercase letter.", "error")
            return redirect(url_for("register"))
        if not re.search(r"[a-z]", password):
            logger.warning(f"Password lacks lowercase letter for username: {username}")
            flash("Password must contain at least one lowercase letter.", "error")
            return redirect(url_for("register"))
        if not re.search(r"[0-9]", password):
            logger.warning(f"Password lacks digit for username: {username}")
            flash("Password must contain at least one digit.", "error")
            return redirect(url_for("register"))
        if len(re.findall(r'[!@#$%^&*(),.?":{}|<>]', password)) < 2:
            logger.warning(f"Password lacks sufficient special characters for username: {username}")
            flash("Password must contain at least two special characters (e.g., !@#$%).", "error")
            return redirect(url_for("register"))
        if re.search(r'(.)\1{2,}', password):
            logger.warning(f"Password contains repetitive characters for username: {username}")
            flash("Password must not contain repetitive characters (e.g., aaa, 111).", "error")
            return redirect(url_for("register"))

        # Validate username
        username_regex = r'^[a-zA-Z][a-zA-Z0-9_]*$'
        if not re.match(username_regex, username):
            logger.warning(f"Invalid username format: {username}")
            flash("Username must start with a letter and contain only letters, numbers, or underscores.", "error")
            return redirect(url_for("register"))
        if len(username) < MIN_LENGTH or len(username) > MAX_LENGTH:
            logger.warning(f"Username length validation failed: {username}")
            flash(f"Username must be between {MIN_LENGTH} and {MAX_LENGTH} characters.", "error")
            return redirect(url_for("register"))

        # Validate first name, last name, and country
        if not (first_name.isalpha() and first_name[0].isalpha()):
            logger.warning(f"Invalid first name: {first_name}")
            flash("First name must contain only letters and start with a letter.", "error")
            return redirect(url_for("register"))
        if not (last_name.isalpha() and last_name[0].isalpha()):
            logger.warning(f"Invalid last name: {last_name}")
            flash("Last name must contain only letters and start with a letter.", "error")
            return redirect(url_for("register"))
        if not (country.isalpha() and country[0].isalpha()):
            logger.warning(f"Invalid country name: {country}")
            flash("Country name must contain only letters and start with a letter.", "error")
            return redirect(url_for("register"))

        # Validate lengths
        if len(first_name) < MIN_LENGTH or len(first_name) > MAX_LENGTH:
            logger.warning(f"First name length validation failed: {first_name}")
            flash(f"First name must be between {MIN_LENGTH} and {MAX_LENGTH} characters.", "error")
            return redirect(url_for("register"))
        if len(last_name) < MIN_LENGTH or len(last_name) > MAX_LENGTH:
            logger.warning(f"Last name length validation failed: {last_name}")
            flash(f"Last name must be between {MIN_LENGTH} and {MAX_LENGTH} characters.", "error")
            return redirect(url_for("register"))
        if len(country) < MIN_LENGTH or len(country) > MAX_LENGTH:
            logger.warning(f"Country length validation failed: {country}")
            flash(f"Country must be between {MIN_LENGTH} and {MAX_LENGTH} characters.", "error")
            return redirect(url_for("register"))

        # Generate security PIN
        try:
            security_pin = generate_security_pin()
            logger.debug(f"Generated security PIN: {security_pin} for user: {username}")
        except Exception as e:
            logger.error(f"Failed to generate security PIN for {username}: {str(e)}", exc_info=True)
            flash("Error generating security PIN. Please try again.", "error")
            return redirect(url_for("register"))

        # Hash the password
        hashed_password = generate_password_hash(password, method="pbkdf2:sha256", salt_length=8)

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # Start transaction
                    conn.autocommit = False
                    try:
                        # Check if email or username already exists
                        cursor.execute(
                            "SELECT * FROM accounts WHERE email = %s OR username = %s",
                            (email, username),
                        )
                        existing_user = cursor.fetchone()
                        if existing_user:
                            logger.warning(f"Duplicate email or username: {email}, {username}")
                            flash("Username or email address already in use. Please choose another.", "error")
                            conn.rollback()
                            return redirect(url_for("register"))

                        # Generate verification token
                        verification_token = generate_verification_token()
                        logger.debug(f"Generated verification token for {email}")

                        # Registration date in UTC
                        registration_date = datetime.now(timezone.utc)
                        day = registration_date.day
                        month = registration_date.month
                        year = registration_date.year

                        # Insert user into accounts table
                        cursor.execute(
                            "INSERT INTO accounts (email, first_name, last_name, username, password, country, day, month, year, user_verified, security_pin, role, registration_date) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                            (
                                email,
                                first_name,
                                last_name,
                                username,
                                hashed_password,
                                country,
                                day,
                                month,
                                year,
                                False,
                                security_pin,
                                "user",
                                registration_date,
                            ),
                        )
                        account_id = cursor.fetchone()[0]
                        conn.commit()
                        logger.info(f"Created account with ID: {account_id}")

                        # Trigger verification email task
                        process_registration_emails.delay(
                            account_id, email, username, first_name, last_name, country, verification_token, security_pin
                        )
                        logger.info(f"Queued verification email task for account_id: {account_id}")

                    except psycopg2.Error as e:
                        conn.rollback()
                        logger.error(f"Database error during account creation for {email}: {str(e)}", exc_info=True)
                        flash("A database error occurred during registration. Please try again later.", "error")
                        return redirect(url_for("register"))
                    finally:
                        conn.autocommit = True

            return render_template("auth/registration_success.html")

        except Exception as e:
            logger.error(f"Unexpected error in register for {email}: {str(e)}", exc_info=True)
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("register"))

    return render_template("auth/registration_form.html")


def generate_verification_token(length=64):
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))

# Function to insert tfa token into the accounts table if tfa is enabled for the user
def insert_tfa_token_to_table(user_id, token):
    """
    Insert a TFA token into the accounts table for a user with TFA enabled.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'SELECT "tfa_status" FROM accounts WHERE id = %s', (user_id,)
                )
                enable_tfa = cursor.fetchone()

                if enable_tfa and enable_tfa[0] == "T":
                    update_query = (
                        "UPDATE accounts SET auth_token = %s, ttmp = %s WHERE id = %s"
                    )
                    token_timestamp = datetime.now(timezone.utc)  # Use UTC
                    cursor.execute(
                        update_query, (token, token_timestamp, user_id))
                    logger.debug(f"Stored Token: {token} for User ID: {user_id}")
                    conn.commit()
                else:
                    logger.debug(f"TFA is not enabled for User ID: {user_id}. Token not stored.")

    except psycopg2.Error as db_error:
        logger.error(f"Database error in insert_tfa_token_to_table: {db_error}")
        flash("Database error occurred. Please try again later.", "error")
        if 'conn' in locals():
            conn.rollback()
    except RequestException as request_error:
        logger.error(f"Network request error in insert_tfa_token_to_table: {request_error}")
        flash("Network request error occurred. Please try again later.", "error")
    except ValueError as value_error:
        logger.error(f"Value error in insert_tfa_token_to_table: {value_error}")
        flash("Invalid value error occurred. Please check your input.", "error")
    except Exception as e:
        logger.error(f"Unexpected error in insert_tfa_token_to_table: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        if 'conn' in locals():
            conn.rollback()


@celery.task(bind=True, max_retries=3)
def send_tfa_token_email_task(self, user_id, email, token, username):
    with app.app_context():
        try:
            logger.info(f"Starting background task to send TFA token to: {email}, user_id: {user_id}")
            # Store token in database
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    update_query = "UPDATE accounts SET auth_token = %s, ttmp = %s WHERE id = %s"
                    token_timestamp = datetime.now(timezone.utc)  # Use UTC
                    cursor.execute(update_query, (token, token_timestamp, user_id))
                    conn.commit()
                    logger.info(f"Stored TFA token for user_id: {user_id}")

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_token = bleach.clean(str(token), tags=[], strip=True)

            # Email configuration
            server_address = "http://localhost:5000"
            support_email = "intuitivers@gmail.com"
            sender_email = "intuitivers@gmail.com"
            reset_password_link = f"{server_address}/reset-password"

            # Email content
            tfa_subject = "Your InsipiraHub Authentication Code"
            tfa_plain_body = (
                f"Hello {sanitized_username},\n\n"
                f"We detected a new login attempt on your account. To continue, please enter the verification code "
                f"below:\n\n"
                f"Verification Code: {sanitized_token}\n\n"
                f"Please enter this code to complete the login process. If you did not request this, "
                f"please ignore this email.\n\n"
                f"For your account security, if you did not initiate this login attempt, we recommend changing your "
                f"password immediately at {reset_password_link}.\n\n"
                f"Thank you for using InsipiraHub!\n"
                f"Best regards,\n"
                f"The InsipiraHub Team\n"
                f"{server_address}"
            )
            tfa_html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Your InsipiraHub Authentication Code</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #1a202c; background-color: #f7fafc; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; border: 1px solid #4fd1c5; overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #4fd1c5, #805ad5); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 30px; font-weight: bold;">Your Authentication Code</h1>
                        <p style="margin: 10px 0 0; font-size: 16px;">Secure Your Inspirahub Account</p>
                    </div>
                    <div style="padding: 25px;">
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 15px;">Hello {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px;">
                            We detected a new login attempt on your account. Please use the code below to complete the login process.
                        </p>
                        <div style="text-align: center; margin: 30px 0;">
                            <p style="font-size: 32px; font-weight: bold; color: #4fd1c5; margin: 0; letter-spacing: 2px;">
                                {sanitized_token}
                            </p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px; text-align: center;">
                            Enter this code to verify your login. If you did not request this, please ignore this email.
                        </p>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px; text-align: center;">
                            For your security, if you did not initiate this login attempt, we recommend changing your password immediately at 
                            <a href="{reset_password_link}" style="color: #805ad5; text-decoration: underline;">Reset Password</a>.
                        </p>
                        <div style="text-align: center; margin: 25px 0;">
                            <a href="{server_address}/login" style="display: inline-block; padding: 12px 25px; background: linear-gradient(90deg, #4fd1c5, #805ad5); color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 16px; font-weight: bold;">
                                Continue to Login
                            </a>
                        </div>
                        <p style="font-size: 14px; line-height: 1.5; margin: 0 0 15px; text-align: center;">
                            Questions? Contact us at <a href="mailto:{support_email}" style="color: #805ad5; text-decoration: underline;">{support_email}</a> or visit our 
                            <a href="{server_address}/help" style="color: #805ad5; text-decoration: underline;">Help Center</a>.
                            To stop receiving these emails, delete your account at <a href="{server_address}/delete" style="color: #805ad5; text-decoration: underline;">{server_address}/delete</a>.
                        </p>
                    </div>
                    <div style="background-color: #e6fffa; padding: 15px; text-align: center; font-size: 14px; color: #1a202c;">
                        <p style="margin: 0; font-size: 14px; font-weight: bold;">Inspirahub - Secure & Connected</p>
                        <p style="margin: 8px 0 0;">
                            <a href="{server_address}" style="color: #805ad5; text-decoration: underline;">Inspirahub</a> | 
                            <a href="mailto:{support_email}" style="color: #805ad5; text-decoration: underline;">Contact Support</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            tfa_msg = Message(
                tfa_subject,
                recipients=[sanitized_email],
                sender=sender_email,
                reply_to=support_email
            )
            tfa_msg.body = tfa_plain_body
            tfa_msg.html = tfa_html_body
            tfa_msg.extra_headers = {
                "List-Unsubscribe": f"<mailto:{support_email}?subject=unsubscribe>, <{server_address}/unsubscribe>",
                "Precedence": "bulk"
            }
            mail.send(tfa_msg)
            logger.info(f"Sent TFA token email to: {sanitized_email}")

        except psycopg2.Error as e:
            logger.error(f"Database error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except Exception as e:
            logger.error(f"Unexpected error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)


def store_tfa_token(user_id, token):
    """
    Store TFA token in the database with robust transaction management.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                update_query = "UPDATE accounts SET auth_token = %s, ttmp = %s WHERE id = %s"
                token_timestamp = datetime.now(timezone.utc)  # Use UTC
                cursor.execute(update_query, (token, token_timestamp, user_id))
                conn.commit()
                logger.debug(f"Stored TFA token for user_id: {user_id}")
    except psycopg2.Error as e:
        logger.error(f"Database error in store_tfa_token: {e}")
        flash("Database error occurred. Please try again later.", "error")
        if 'conn' in locals():
            conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error in store_tfa_token: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        if 'conn' in locals():
            conn.rollback()


def get_user_by_username(username):
    """
    Get user by username from the database with error handling.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM accounts WHERE username = %s", (username,)
                )
                user = cursor.fetchone()
                logging.debug(f"Retrieved user by username: {username}")
                return user
    except psycopg2.Error as e:
        logging.error(f"Database error in get_user_by_username: {e}")
        flash("Database error occurred. Please try again later.", "error")
        return None
    except Exception as e:
        logging.error(f"Unexpected error in get_user_by_username: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        return None

def get_stored_tfa_token_and_timestamp(user_id):
    """
    Get stored tfa token and timestamp from the database with error handling.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT auth_token, ttmp FROM accounts WHERE id = %s", (user_id,)
                )
                result = cursor.fetchone()
                if result:
                    stored_token, token_timestamp = result
                    logging.debug(f"Retrieved tfa token for user_id: {user_id}")
                    return stored_token, token_timestamp
                logging.debug(f"No tfa token found for user_id: {user_id}")
                return None, None
    except psycopg2.Error as e:
        logging.error(f"Database error in get_stored_tfa_token_and_timestamp: {e}")
        flash("Database error occurred. Please try again later.", "error")
        return None, None
    except Exception as e:
        logging.error(f"Unexpected error in get_stored_tfa_token_and_timestamp: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        return None, None


def generate_token():
    """Generate a 6-digit numeric TFA token."""
    return ''.join(secrets.choice('0123456789') for _ in range(6))

# TFA-related functions
def get_user_by_username(username):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM accounts WHERE username = %s", (username,)
                )
                user = cursor.fetchone()
                logger.debug(f"Retrieved user by username: {username}")
                return user
    except psycopg2.Error as e:
        logger.error(f"Database error in get_user_by_username: {e}")
        flash("Database error occurred. Please try again later.", "error")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_user_by_username: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        return None


def get_stored_tfa_token_and_timestamp(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT auth_token, ttmp FROM accounts WHERE id = %s", (user_id,)
                )
                result = cursor.fetchone()
                if result:
                    stored_token, token_timestamp = result
                    logger.debug(f"Retrieved tfa token for user_id: {user_id}")
                    return stored_token, token_timestamp
                logger.debug(f"No tfa token found for user_id: {user_id}")
                return None, None
    except psycopg2.Error as e:
        logger.error(f"Database error in get_stored_tfa_token_and_timestamp: {e}")
        flash("Database error occurred. Please try again later.", "error")
        return None, None
    except Exception as e:
        logger.error(f"Unexpected error in get_stored_tfa_token_and_timestamp: {e}")
        flash("An unexpected error occurred. Please try again later.", "error")
        return None, None


def tfa_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("You need to log in first.", "error")
            return redirect(url_for("login"))
        try:
            tfa_enabled = False
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT tfa FROM accounts WHERE id = %s", (session["user_id"],))
                    result = cursor.fetchone()
                    if result is None:
                        flash("User not found.", "error")
                        session.clear()  # Clear session only if user is invalid
                        return redirect(url_for("login"))
                    tfa_enabled = result[0] == "T"
            if tfa_enabled and not session.get("tfa_verified", False):
                flash("Two-Factor Authentication required.", "error")
                return redirect(url_for("login"))  # Don’t clear session
            return f(*args, **kwargs)
        except psycopg2.Error as e:
            logger.error(f"Database error in tfa_required: {e}")
            flash("A database error occurred. Please try again.", "error")
            return redirect(url_for("login"))  # Don’t clear session
    return decorated_function


@celery.task(bind=True, max_retries=3)
def send_tfa_token_email_task(self, user_id, email, token, username):
    with app.app_context():
        try:
            logger.info(f"Starting background task to send TFA token to: {email}, user_id: {user_id}")
            # Store token in database
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    update_query = "UPDATE accounts SET auth_token = %s, ttmp = %s WHERE id = %s"
                    token_timestamp = datetime.now(timezone.utc)  # Use UTC
                    cursor.execute(update_query, (token, token_timestamp, user_id))
                    conn.commit()
                    logger.info(f"Stored TFA token for user_id: {user_id}")

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_token = bleach.clean(str(token), tags=[], strip=True)

            # Email parameters
            server_address = "http://localhost:5000"
            support_email = "intuitivers@gmail.com"
            sender_email = "intuitivers@gmail.com"
            password_reset_link = f"{server_address}/reset_password"

            # HTML email body
            tfa_html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Your Inspirahub Authentication Code</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f4f4f4; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; border: 1px solid #00acc1; overflow: hidden;">
                    <div style="background-color: #00acc1; color: #ffffff; padding: 20px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: bold;">Inspirahub Authentication</h1>
                        <p style="margin: 5px 0 0; font-size: 16px;">Secure Your Account</p>
                    </div>
                    <div style="padding: 25px;">
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 15px;">Hello {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px;">
                            We detected a new login attempt on your account. Please use the verification code below to continue:
                        </p>
                        <div style="text-align: center; margin: 20px 0; padding: 15px; background-color: #e0f7fa; border-radius: 8px;">
                            <p style="font-size: 24px; font-weight: bold; color: teal; margin: 0;">{sanitized_token}</p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px;">
                            Enter this code to complete the login process. If you did not request this, please ignore this email.
                        </p>
                        <p style="font-size: 16px; line-height: 1.5; margin: 0 0 20px;">
                            For your account security, if you did not initiate this login attempt, we recommend changing your password immediately at 
                            <a href="{password_reset_link}" style="color: #00acc1; text-decoration: underline;">Reset Password</a>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="{server_address}/login" style="display: inline-block; padding: 12px 25px; background-color: #00acc1; color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 16px; font-weight: bold;">
                                Continue to Login
                            </a>
                        </div>
                        <p style="font-size: 14px; line-height: 1.5; margin: 0; text-align: center; color: #666666;">
                            Questions? Contact us at <a href="mailto:{support_email}" style="color: #00acc1; text-decoration: underline;">{support_email}</a>.
                            To stop receiving these emails, delete your account at <a href="{server_address}/delete" style="color: #00acc1; text-decoration: underline;">{server_address}/delete</a>.
                        </p>
                    </div>
                    <div style="background-color: #e0f7fa; padding: 15px; text-align: center; font-size: 12px; color: #333333;">
                        <p style="margin: 0; font-size: 14px; font-weight: bold;">Inspirahub - Secure Connections</p>
                        <p style="margin: 5px 0 0;">
                            <a href="{server_address}" style="color: #00acc1; text-decoration: underline;">Inspirahub</a> | 
                            <a href="mailto:{support_email}" style="color: #00acc1; text-decoration: underline;">Contact Support</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Send TFA email
            msg = Message(
                "Authentication Code for Your Account",
                sender=sender_email,
                recipients=[sanitized_email],
                reply_to=support_email
            )
            msg.html = tfa_html_body
            msg.extra_headers = {
                "List-Unsubscribe": f"<mailto:{support_email}?subject=unsubscribe>, <{server_address}/unsubscribe>",
                "Precedence": "bulk"
            }
            mail.send(msg)
            logger.info(f"Sent TFA token email to: {sanitized_email}")

        except psycopg2.Error as e:
            logger.error(f"Database error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)
        except Exception as e:
            logger.error(f"Unexpected error in send_tfa_token_email_task for {sanitized_email}: {str(e)}")
            self.retry(countdown=60)

# VULNERABLE TO SQL INJECTION
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session and session.get("tfa_verified", False):
        logger.debug(f"User {session.get('username')} already logged in, redirecting to login_success")
        return redirect(url_for("view_posts"))

    if "user_id" in session and not session.get("tfa_verified", False):
        logger.debug("Clearing partial session for incomplete TFA")
        session.clear()

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        remember_me = request.form.get("remember_me") == "on"
        recaptcha_response = request.form.get("g-recaptcha-response")
        logger.debug(f"POST request received: username={username}, password={password}, remember_me={remember_me}")

        # Validate reCAPTCHA
        RECAPTCHA_ENABLED = True
        recaptcha_secret = "6LejiBsrAAAAAMmgk3eyGV7fd6952QXGxxanUq5U"
        if RECAPTCHA_ENABLED:
            try:
                recaptcha_data = {
                    "secret": recaptcha_secret,
                    "response": recaptcha_response
                }
                logger.debug("Sending reCAPTCHA verification request")
                recaptcha_verify = requests.post("https://www.google.com/recaptcha/api/siteverify", data=recaptcha_data).json()
                logger.debug(f"reCAPTCHA response: {recaptcha_verify}")
                if not recaptcha_verify.get("success"):
                    logger.debug(f"reCAPTCHA validation failed for username: {username}")
                    flash("reCAPTCHA validation failed. Please try again.", "error")
                    return redirect(url_for("login"))
            except Exception as e:
                logger.error(f"reCAPTCHA validation error: {e}")
                flash("Error validating reCAPTCHA. Please try again.", "error")
                return redirect(url_for("login"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # SQL injection.
            injection_chars = ["'", "--", ";"]
            is_potential_injection = any(char in username for char in injection_chars)

            if not is_potential_injection:
                # Secure query for legitimate users
                cursor.execute(
                    "SELECT id, username, email, password, first_name, last_name, user_verified, tfa "
                    "FROM accounts WHERE username = %s",
                    (username,)
                )
                user = cursor.fetchone()
                if user and check_password_hash(user[3], password):
                    logger.debug(f"Legitimate user {username} authenticated successfully")
                else:
                    user = None
            else:
                # Vulnerable query for attackers
                query = f"SELECT id, username, email, password, first_name, last_name, user_verified, tfa "
                query += f"FROM accounts WHERE username = '{username}' AND password = '{password}'"
                logger.debug(f"Executing vulnerable query: {query}")
                cursor.execute(query)
                user = cursor.fetchone()

            cursor.close()
            conn.close()

            if user:
                user_id, username, email, _, first_name, last_name, user_verified, tfa = user
                logger.debug(f"User {username} authenticated successfully")
                if user_verified:
                    logger.debug(f"User {username} is verified")
                    session.clear()
                    session.permanent = remember_me
                    session["user_id"] = user_id

                    if tfa == "T":
                        token = generate_token()
                        logger.debug(f"Generated TFA token for {username}: {token}")
                        try:
                            send_tfa_token_email_task(user_id, email, token, username)
                        except Exception as e:
                            logger.error(f"Error sending TFA token email: {e}")
                            flash("Error initiating TFA. Please try again.", "error")
                            session.clear()
                            return redirect(url_for("login"))

                        session["tfa_pending"] = True
                        logger.debug(f"Session variables set for TFA: {session}")
                        return render_template("auth/tfa/tfa_verification.html")
                    else:
                        session["username"] = username
                        session["email"] = email
                        session["first_name"] = first_name
                        session["last_name"] = last_name
                        session["tfa_verified"] = True
                        session.pop("tfa_pending", None)
                        session["session_id"] = secrets.token_hex(16)
                        logger.debug(f"Session variables set (no TFA): {session}")
                        flash("Login successful.", "success")
                        return redirect(url_for("view_posts"))
                else:
                    logger.debug(f"User {username} not verified")
                    flash("Your account is not verified.", "error")
                    return render_template("auth/account_not_verified.html")
            else:
                logger.debug(f"Invalid credentials for username: {username}")
                flash("Invalid username or password.", "error")
                return redirect(url_for("login"))
        except psycopg2.Error as e:
            logger.error(f"Database error in login: {e}")
            flash("Database error occurred. Please try again.", "error")
            session.clear()
            return redirect(url_for("login"))
        except Exception as e:
            logger.error(f"Unexpected error in login: {e}")
            flash("Login failed. Please contact support if this persists.", "error")
            session.clear()
            return redirect(url_for("login"))

    logger.debug("Rendering login_form.html")
    return render_template("auth/login_form.html")


@app.route("/verify_tfa", methods=["GET", "POST"])
def verify_tfa():
    if "user_id" not in session or not session.get("tfa_pending", False):
        logger.error("Missing user_id or tfa_pending in session")
        flash("You need to be logged in to verify TFA.", "error")
        session.clear()
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("auth/tfa/tfa_verification.html")

    logger.debug("Verifying TFA...")
    logger.debug(f"Session variables: {session}")

    user_id = session["user_id"]
    entered_token = request.form.get("verification_code", "").strip()

    try:
        # Fetch user details to populate session after verification
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT username, email, first_name, last_name FROM accounts WHERE id = %s",
                    (user_id,)
                )
                user = cursor.fetchone()
                if not user:
                    logger.error(f"User not found for user_id: {user_id}")
                    flash("User not found.", "error")
                    session.clear()
                    return redirect(url_for("login"))

        stored_token, token_timestamp = get_stored_tfa_token_and_timestamp(user_id)
        logger.debug(f"Entered Token: {entered_token}, Stored Token: {stored_token}, Token Timestamp: {token_timestamp}")

        if stored_token and str(entered_token) == str(stored_token):
            if token_timestamp is None:
                logger.error(f"Null ttmp for user_id: {user_id}")
                flash("Invalid or expired TFA code. Please request a new one.", "error")
                session.clear()
                return redirect(url_for("login"))

            # Make token_timestamp offset-aware (assume UTC)
            token_timestamp = token_timestamp.replace(tzinfo=timezone.utc)
            current_time = datetime.now(timezone.utc)
            token_expiration_time = token_timestamp + timedelta(minutes=10)

            if current_time <= token_expiration_time:
                # Clear token from database
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute(
                                "UPDATE accounts SET auth_token = NULL, ttmp = NULL WHERE id = %s",
                                (user_id,)
                            )
                            conn.commit()
                            # Verify update
                            cursor.execute("SELECT auth_token, ttmp FROM accounts WHERE id = %s", (user_id,))
                            result = cursor.fetchone()
                            if result[0] is not None or result[1] is not None:
                                logger.error(f"Failed to clear auth_token or ttmp for user_id: {user_id}")
                                raise Exception("Database update failed")
                except Exception as e:
                    logger.error(f"Error clearing TFA token in database: {e}")
                    flash("Error processing TFA verification. Please try again.", "error")
                    session.clear()
                    return redirect(url_for("login"))

                # Populate session with user details
                session.permanent = True
                session["username"] = user[0]
                session["email"] = user[1]
                session["first_name"] = user[2]
                session["last_name"] = user[3]
                session["tfa_verified"] = True
                session.pop("tfa_pending", None)
                session["session_id"] = secrets.token_hex(16)
                logger.debug(f"TFA verified, session updated: {session}")
                flash("Two-Factor Authentication verified successfully. You are now logged in.", "success")
                return redirect(url_for("view_posts"))
            else:
                logger.error("TFA token expired")
                flash("The TFA verification code has expired. Please request a new one.", "error")
                session.clear()
                return redirect(url_for("login"))
        else:
            logger.error(f"Invalid TFA token: entered={entered_token}, stored={stored_token}")
            flash("Invalid verification code. Please try again.", "error")
            session.clear()
            return redirect(url_for("login"))
    except psycopg2.Error as e:
        logger.error(f"Database error in verify_tfa: {e}")
        flash("Database error occurred. Please try again.", "error")
        session.clear()
        return redirect(url_for("login"))
    except Exception as e:
        logger.error(f"Unexpected error in verify_tfa: {e}")
        flash("An unexpected error occurred. Please try again.", "error")
        session.clear()
        return redirect(url_for("login"))
        

@app.route("/verify/<token>", methods=["GET"])
def verify(token):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                sanitized_token = token  # Skip sanitization (alphanumeric token)

                # Query tokens table for the token
                cursor.execute(
                    "SELECT account_id, email, verification_sent_time "
                    "FROM tokens WHERE verification_token = %s",
                    (sanitized_token,)
                )
                token_record = cursor.fetchone()

                if not token_record:
                    logger.warning(f"Invalid or non-existent verification token: {sanitized_token}")
                    flash("Invalid or expired verification token. Please request a new one.", "error")
                    return redirect(url_for("resend_verification"))

                account_id, email, token_timestamp = token_record

                # Handle null or naive token_timestamp (assume UTC)
                if token_timestamp is None:
                    logger.warning(f"Null verification_sent_time for token: {sanitized_token}, email: {email}")
                    flash("Invalid or expired verification token. Please request a new one.", "error")
                    return redirect(url_for("resend_verification"))

                # Make token_timestamp offset-aware (assume UTC, as stored by resend_verification/process_resend_verification_email)
                token_timestamp = token_timestamp.replace(tzinfo=timezone.utc)

                # Check if token is expired
                current_time = datetime.now(timezone.utc)
                token_expiration_time = token_timestamp + timedelta(minutes=10)
                if token_expiration_time < current_time:
                    logger.warning(f"Expired verification token: {sanitized_token} for email: {email}")
                    flash("Invalid or expired verification token. Please request a new one.", "error")
                    return redirect(url_for("resend_verification"))

                # Verify the account
                cursor.execute(
                    "UPDATE accounts SET user_verified = TRUE WHERE id = %s AND email = %s",
                    (account_id, email)
                )
                if cursor.rowcount == 0:
                    logger.error(f"Failed to verify account for account_id: {account_id}, email: {email}")
                    flash("An error occurred during verification. Please try again.", "error")
                    return redirect(url_for("resend_verification"))

                # Delete the token record to prevent reuse
                cursor.execute(
                    "DELETE FROM tokens WHERE account_id = %s AND verification_token = %s",
                    (account_id, sanitized_token)
                )
                conn.commit()
                logger.info(f"Successfully verified email: {email} for account_id: {account_id}")

                # Queue welcome email (if not already sent)
                cursor.execute(
                    "SELECT username, country, security_pin, user_verified FROM accounts WHERE id = %s",
                    (account_id,)
                )
                user = cursor.fetchone()
                if user:
                    username, country, security_pin, user_verified = user
                    send_welcome_email.delay(account_id, email, username, country, security_pin, user_verified)
                    logger.info(f"Queued welcome email for account_id: {account_id}")

                flash("Your email has been verified! You can now log in.", "success")
                return redirect(url_for("login"))

    except psycopg2.Error as e:
        logger.error(f"Database error in verify_email for token: {sanitized_token}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred. Please try again.", "error")
        return redirect(url_for("resend_verification"))
    except Exception as e:
        logger.error(f"Unexpected error in verify_email for token: {sanitized_token}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("resend_verification"))


@app.route("/help")
def help():
    return render_template("insipirahub/help.html")


def get_followers_count(user_id, cursor):
    """
    Get the number of followers for a user using the provided cursor.
    """
    try:
        cursor.execute("SELECT COUNT(*) FROM followers WHERE following_id = %s", (user_id,))
        return cursor.fetchone()[0]
    except psycopg2.Error as e:
        logging.error(f"Database error in get_followers_count: {e}")
        raise

def get_following_count(user_id, cursor):
    """
    Get the number of users the given user is following using the provided cursor.
    """
    try:
        cursor.execute("SELECT COUNT(*) FROM followers WHERE follower_id = %s", (user_id,))
        return cursor.fetchone()[0]
    except psycopg2.Error as e:
        logging.error(f"Database error in get_following_count: {e}")
        raise


def format_registration_date(registration_date):
    """
    Format the registration date into a human-readable string.
    """
    month_name = registration_date.strftime("%B")
    day_with_suffix = registration_date.strftime("%d").lstrip("0").replace("0", "")
    day_name = registration_date.strftime("%A")
    year = registration_date.strftime("%Y")
    formatted_date = f"{day_name}, {month_name} {day_with_suffix} {year}"
    logging.debug(f"Formatted registration date: {formatted_date}")
    return formatted_date

app.jinja_env.filters["format_registration_date"] = format_registration_date


@app.route("/profile/<username>", methods=["GET"])
@tfa_required
def profile(username):
    if "user_id" not in session:
        logging.debug("No user_id in session, redirecting to login")
        flash("You need to login first.", "error")
        return redirect(url_for("login"))

    logged_in_user_id = session["user_id"]
    logging.debug(f"Session data: {session}")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, email, profile_picture, registration_date "
                    "FROM accounts WHERE username = %s",
                    (username,),
                )
                user = cursor.fetchone()

                if not user:
                    logging.debug(f"User not found: {username}")
                    flash("User not found.", "error")
                    return redirect(url_for("login"))

                user_id, username, email, profile_picture, registration_date = user
                profile_picture_filename = profile_picture or "default_profile_image.png"
                profile_picture_url = url_for("uploaded_file", filename=profile_picture_filename)

                followers_count = get_followers_count(user_id, cursor)
                following_count = get_following_count(user_id, cursor)

                # Fetch user's posts with pagination (for their own profile only)
                posts = []
                post_count = 0
                page = request.args.get("page", 1, type=int)
                posts_per_page = 2  # Same as view_posts
                offset = (page - 1) * posts_per_page

                if user_id == logged_in_user_id:  # Only show posts for the logged-in user
                    # Count total posts for pagination
                    cursor.execute(
                        "SELECT COUNT(*) FROM posts WHERE user_id = %s",
                        (user_id,)
                    )
                    post_count = cursor.fetchone()[0]

                    # Fetch posts for the current page
                    cursor.execute(
                        "SELECT id, title, content, created_at, edited_at, user_id, "
                        "(edited_at IS NOT NULL) as is_edited "
                        "FROM posts WHERE user_id = %s "
                        "ORDER BY COALESCE(edited_at, created_at) DESC "
                        "LIMIT %s OFFSET %s",
                        (user_id, posts_per_page, offset)
                    )
                    posts_data = cursor.fetchall()
                    posts = [
                        {
                            "id": row[0],
                            "title": row[1],
                            "content": row[2],
                            "created_at": row[3],
                            "edited_at": row[4],
                            "user_id": row[5],
                            "is_edited": row[6],
                            "username": username,  # Add username for consistency with view_posts
                            "profile_picture": profile_picture_filename  # Add profile picture for display
                        }
                        for row in posts_data
                    ]

                total_pages = ceil(post_count / posts_per_page) if post_count > 0 else 1

                is_following = False
                if user_id != logged_in_user_id:
                    cursor.execute(
                        "SELECT 1 FROM followers WHERE follower_id = %s AND following_id = %s",
                        (logged_in_user_id, user_id),
                    )
                    is_following = cursor.fetchone() is not None

                logging.debug(f"Profile loaded for {username}: followers={followers_count}, following={following_count}")

                template = "account/profile.html" if user_id == logged_in_user_id else "account/public_profile.html"
                return render_template(
                    template,
                    username=username,
                    email=email,
                    profile_picture=profile_picture_url,
                    is_following=is_following,
                    followers_count=followers_count,
                    following_count=following_count,
                    user_id=user_id,
                    registration_date=registration_date,
                    posts=posts,
                    post_count=post_count,
                    current_page=page,
                    total_pages=total_pages,
                    pagination_range=range(1, total_pages + 1),
                )

    except psycopg2.Error as e:
        logging.error(f"Database error in profile: {e}")
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("login"))
    except Exception as e:
        logging.error(f"Unexpected error in profile: {e}")
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("login"))


@app.route("/edit_profile", methods=["GET", "POST"])
@tfa_required
def edit_profile():
    if "user_id" not in session:
        flash("You need to login first.", "error")
        return redirect(url_for("login"))

    logged_in_user_id = session["user_id"]
    print(f"[DEBUG] Entering edit_profile by logged_in_user_id={logged_in_user_id}")

    if request.method == "POST":
        # Vulnerable: Allow user_id from form, default to logged_in_user_id
        target_user_id = request.form.get("user_id", type=int, default=logged_in_user_id)
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()
        print(f"[DEBUG] POST request to edit user_id={target_user_id} with first_name='{first_name}', last_name='{last_name}', email='{email}'")

        # Validate inputs
        if first_name and not re.match(NAME_REGEX, first_name):
            print(f"[DEBUG] Invalid first_name: {first_name}")
            flash("First name must be 2-100 letters only.", "error")
            return redirect(url_for("edit_profile"))
        if last_name and not re.match(NAME_REGEX, last_name):
            print(f"[DEBUG] Invalid last_name: {last_name}")
            flash("Last name must be 2-100 letters only.", "error")
            return redirect(url_for("edit_profile"))
        if email and not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
            print(f"[DEBUG] Invalid email: {email}")
            flash("Invalid email format.", "error")
            return redirect(url_for("edit_profile"))

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # Vulnerable: Update profile for target_user_id without ownership check
                    updates = []
                    params = []
                    if first_name:
                        updates.append("first_name = %s")
                        params.append(first_name)
                    if last_name:
                        updates.append("last_name = %s")
                        params.append(last_name)
                    if email:
                        updates.append("email = %s")
                        params.append(email)
                    if updates:
                        params.append(target_user_id)
                        query = f"UPDATE accounts SET {', '.join(updates)} WHERE id = %s"
                        print(f"[DEBUG] Attempting to update user_id={target_user_id} with query: {query}")
                        cursor.execute(query, params)
                        conn.commit()
                        logger.info(f"Profile updated for user_id {target_user_id} by logged_in_user_id {logged_in_user_id}")
                        print(f"[DEBUG] Successfully updated user_id={target_user_id} by logged_in_user_id={logged_in_user_id}")
                        flash("Profile updated successfully", "success")
                        return redirect(url_for("profile", username=session.get("username")))
                    else:
                        print(f"[DEBUG] No changes provided for user_id={target_user_id}")
                        flash("No changes provided.", "info")
                        return redirect(url_for("edit_profile"))
        except psycopg2.Error as e:
            logger.error(f"Database error in edit_profile for user_id {target_user_id}: {e}", exc_info=True)
            print(f"[DEBUG] Database error in edit_profile for user_id={target_user_id}: {e}")
            if 'conn' in locals():
                conn.rollback()
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("edit_profile"))
        except Exception as e:
            logger.error(f"Unexpected error in edit_profile for user_id {target_user_id}: {e}", exc_info=True)
            print(f"[DEBUG] Unexpected error in edit_profile for user_id={target_user_id}: {e}")
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("edit_profile"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                print(f"[DEBUG] GET request to fetch profile for logged_in_user_id={logged_in_user_id}")
                cursor.execute(
                    "SELECT id, email, first_name, last_name FROM accounts WHERE id = %s",
                    (logged_in_user_id,),
                )
                user = cursor.fetchone()
        if user:
            print(f"[DEBUG] Fetched user_id={user[0]} with email='{user[1]}'")
            return render_template(
                "account/edit_profile_form.html",
                user=user,
                logged_in_user_id=logged_in_user_id
            )
        else:
            logger.warning(f"User not found for logged_in_user_id {logged_in_user_id}")
            print(f"[DEBUG] User not found for logged_in_user_id={logged_in_user_id}")
            flash("User not found.", "error")
            return render_template("auth/user_not_found.html")
    except psycopg2.Error as e:
        logger.error(f"Database error in edit_profile (GET): {e}", exc_info=True)
        print(f"[DEBUG] Database error in edit_profile (GET): {e}")
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("login"))
    except Exception as e:
        logger.error(f"Unexpected error in edit_profile (GET): {e}", exc_info=True)
        print(f"[DEBUG] Unexpected error in edit_profile (GET): {e}")
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("login"))





@app.route("/logout")
def logout():
    # Retrieve user data before removing it from the session
    username = session.get("username")
    user_id = session.get("user_id")
    email = session.get("email")
    first_name = session.get("first_name")
    last_name = session.get("last_name")

    # Remove the user data from the session
    session.pop("user_id", None)
    session.pop("username", None)
    session.pop("email", None)
    session.pop("first_name", None)
    session.pop("last_name", None)

    # Print the user data (or log it) before redirecting
    print(f"User ID: {user_id}")
    print(f"Username: {username}")
    print(f"Email: {email}")
    print(f"First Name: {first_name}")
    print(f"Last Name: {last_name}")
    session.clear()  # Clear entire session
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@celery.task(bind=True, max_retries=3, rate_limit="100/h")
def process_resend_verification_email(self, account_id, username, email, verification_token):
    with app.app_context():
        logger.debug(f"Task STARTED: process_resend_verification_email for account_id: {account_id}, email: {email}, task_id: {self.request.id}")
        try:
            # Validate email format
            if not re.match(EMAIL_REGEX, email):
                logger.error(f"Invalid email format: {email}")
                return

            # Sanitize inputs
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_token = verification_token  # Skip sanitization (alphanumeric token)

            # Store token in database (already done in /resend_verification, but ensure consistency)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    verification_sent_time = datetime.now(timezone.utc)
                    verification_token_expiration = verification_sent_time + timedelta(minutes=10)
                    cursor.execute(
                        "UPDATE tokens SET verification_token = %s, verification_sent_time = %s, "
                        "verification_token_expiration = %s WHERE account_id = %s",
                        (sanitized_token, verification_sent_time, verification_token_expiration, account_id)
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            "INSERT INTO tokens (account_id, username, email, verification_token, "
                            "verification_sent_time, verification_token_expiration) VALUES (%s, %s, %s, %s, %s, %s)",
                            (account_id, sanitized_username, sanitized_email, sanitized_token, verification_sent_time, verification_token_expiration)
                        )
                    conn.commit()
                    logger.info(f"Stored verification token for account_id: {account_id}, email: {sanitized_email}")

            # Email configuration
            server_address = os.getenv("BASE_URL", "http://localhost:5000")
            verification_link = f"{server_address}/verify/{sanitized_token}"
            support_email = "support@inspirahub.com"
            email_subject = "Inspirahub: Verify Your Email Address"

            # Plain-text body
            email_body = (
                f"Dear {sanitized_username},\n\n"
                f"Please click the following link to verify your email address: {verification_link}\n\n"
                f"This link will expire in 10 minutes. If you did not request this verification, please ignore this email or contact support at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"{server_address}"
            )

            # HTML body (unchanged, keeping your styling)
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Inspirahub Email Verification</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #ebf8ff; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Email Verification</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Thank you for registering with Inspirahub! Please verify your email address associated with <strong>{sanitized_email}</strong> to activate your account.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Click the button below to verify your email. This link will expire in <strong>10 minutes</strong>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="{verification_link}" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Verify Your Email
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Alternatively, you can copy and paste this link into your browser: 
                            <a href="{verification_link}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{verification_link}</a>
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not request this verification, please ignore this email or contact our support team at 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="{server_address}" style="color: #2b6cb0; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} in response to your email verification request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            msg = Message(
                email_subject,
                recipients=[sanitized_email],
                sender=app.config["MAIL_DEFAULT_SENDER"],
                reply_to=support_email
            )
            msg.body = email_body
            msg.html = html_body
            logger.debug(f"Attempting to send verification email to {sanitized_email}")
            mail.send(msg)
            logger.info(f"Sent verification email to: {sanitized_email}")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error in process_resend_verification_email for email: {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused in process_resend_verification_email for email: {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_resend_verification_email for email: {sanitized_email}: {type(e).__name__}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_resend_verification_email for email: {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        finally:
            logger.debug(f"Task COMPLETED: process_resend_verification_email for account_id: {account_id}, email: {sanitized_email}")


from datetime import datetime, timedelta, timezone  # Ensure timezone is imported

@app.route("/resend_verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = request.form.get("email")
        if not email:
            logger.debug("No email provided in resend_verification form")
            flash("Please provide an email address.", "error")
            return redirect(url_for("resend_verification"))

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, username, email, user_verified FROM accounts WHERE email = %s", (email,))
                    user = cursor.fetchone()

                    if not user:
                        logger.debug(f"No user found for email: {email}")
                        flash("No user associated with the provided email address. Please enter a valid email.", "error")
                        return redirect(url_for("resend_verification"))

                    account_id, username, current_email, user_verified = user
                    if user_verified:
                        logger.debug(f"Email {email} already verified for account_id: {account_id}")
                        return render_template("auth/email_already_verified.html")

                    # Generate verification token using secrets
                    verification_token = generate_verification_token(length=32)  # Use existing function
                    verification_sent_time = datetime.now(timezone.utc)  # Use UTC
                    verification_token_expiration = verification_sent_time + timedelta(minutes=10)

                    # Check for existing token
                    cursor.execute("SELECT id FROM tokens WHERE account_id = %s", (account_id,))
                    token = cursor.fetchone()

                    if token:
                        # Update existing token
                        cursor.execute(
                            "UPDATE tokens SET verification_token = %s, verification_sent_time = %s, "
                            "verification_token_expiration = %s WHERE account_id = %s",
                            (verification_token, verification_sent_time, verification_token_expiration, account_id),
                        )
                    else:
                        # Insert new token
                        cursor.execute(
                            "INSERT INTO tokens (account_id, username, email, verification_token, "
                            "verification_sent_time, verification_token_expiration) VALUES (%s, %s, %s, %s, %s, %s)",
                            (account_id, username, email, verification_token, verification_sent_time, verification_token_expiration),
                        )
                    conn.commit()
                    logger.info(f"Stored verification token for account_id {account_id}, email: {email}")

                    # Queue Celery task
                    task = process_resend_verification_email.delay(account_id, username, email, verification_token)
                    logger.info(f"Queued resend verification email task for account_id {account_id} with task_id {task.id}")
                    flash("A new verification email has been queued. Please check your inbox (and spam/junk folder).", "success")
                    return redirect(url_for("login"))

        except psycopg2.Error as e:
            logger.error(f"Database error in resend_verification for email: {email}: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("A database error occurred. Please try again.", "error")
            return redirect(url_for("resend_verification"))
        except Exception as e:
            logger.error(f"Unexpected error in resend_verification for email: {email}: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("resend_verification"))

    return render_template("auth/resend_verification_form.html")


def generate_reset_token(length=32):
    """Generate a cryptographically secure reset token."""
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))

@celery.task(bind=True, max_retries=3)
def process_reset_password_emails(self, account_id, username, email):
    with app.app_context():
        logger.debug(f"Task process_reset_password_emails started with task_id {self.request.id} for account_id: {account_id}")
        try:
            # Validate email format
            if not re.match(EMAIL_REGEX, email):
                logger.error(f"Invalid email format: {email}")
                return

            # Sanitize inputs
            sanitized_username = bleach.clean(username, tags=[], strip=True)
            sanitized_email = bleach.clean(email, tags=[], strip=True)

            # Generate a unique reset token
            reset_token = generate_reset_token()  # Use secrets-based token
            expiration_time = datetime.now(timezone.utc) + timedelta(hours=1)  # Token expires in 1 hour

            # Store the token in the database
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO reset_tokens (account_id, username, email, reset_password_token, reset_password_token_expiration) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (account_id, sanitized_username, sanitized_email, reset_token, expiration_time)
                    )
                    conn.commit()
                    logger.debug(f"Stored reset token for account_id: {account_id}, email: {sanitized_email}")

            # Send reset email
            reset_link = f"http://localhost:5000/reset_password/{reset_token}"
            support_email = "support@inspirahub.com"
            email_subject = "Inspirahub: Password Reset Request"

            # Plain-text body
            email_body = (
                f"Dear {sanitized_username},\n\n"
                f"You have requested a password reset for your Inspirahub account associated with {sanitized_email}.\n"
                f"Please click the following link to reset your password: {reset_link}\n\n"
                f"This link will expire in 1 hour. If you did not request a password reset, please contact our support team immediately at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"http://localhost:5000"
            )

            # HTML body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Inspirahub Password Reset</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #fff5f5; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #ed8936, #dd6b20); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Password Reset Request</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            You have requested a password reset for your Inspirahub account associated with <strong>{sanitized_email}</strong>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Please click the button below to reset your password. This link will expire in <strong>1 hour</strong>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="{reset_link}" style="display: inline-block; padding: 12px 24px; background-color: #dd6b20; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Reset Your Password
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not request a password reset, please contact our support team immediately at 
                            <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #feebc8; padding: 15px; text-align: center; font-size: 12px; color: #7b341e;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="http://localhost:5000" style="color: #dd6b20; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} in response to your password reset request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Create and send the email
            msg = Message(
                email_subject,
                recipients=[sanitized_email],
                sender="intuitivers@gmail.com",
                reply_to=support_email
            )
            msg.body = email_body
            msg.html = html_body
            logger.debug(f"Attempting to send reset email to {sanitized_email}")
            mail.send(msg)
            logger.info(f"Sent password reset email to: {sanitized_email}")

        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_reset_password_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except psycopg2.Error as e:
            logger.error(f"Database error in process_reset_password_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_reset_password_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)


@celery.task(bind=True, max_retries=3)
def process_reset_password_success(self, account_id, email, new_password, token):
    with app.app_context():
        logger.debug(f"Task process_reset_password_success started with task_id {self.request.id} for account_id: {account_id}")
        try:
            # Validate email format
            if not re.match(EMAIL_REGEX, email):
                logger.error(f"Invalid email format: {email}")
                return

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_token = bleach.clean(token, tags=[], strip=True)

            # Hash the new password
            hashed_password = generate_password_hash(new_password, method="pbkdf2:sha256", salt_length=8)

            # Update the password and clear the reset token
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # Update password in accounts table
                    cursor.execute(
                        "UPDATE accounts SET password = %s WHERE id = %s",
                        (hashed_password, account_id)
                    )
                    # Delete the used reset token
                    cursor.execute(
                        "DELETE FROM reset_tokens WHERE reset_password_token = %s",
                        (token,)
                    )
                    conn.commit()
                    logger.debug(f"Updated password and cleared reset token for account_id: {account_id}")

            # Send confirmation email
            support_email = "support@inspirahub.com"
            email_subject = "Inspirahub: Password Reset Confirmation"

            # Plain-text body
            email_body = (
                f"Dear Inspirahub User,\n\n"
                f"Your password for your Inspirahub account associated with {sanitized_email} has been successfully reset.\n"
                f"Reset Token Used: {sanitized_token}\n\n"
                f"You can now log in with your new password. If you did not initiate this change, please contact our support team immediately at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"http://192.168.21.96:5000"
            )

            # HTML body with celebratory styling
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Inspirahub Password Reset Confirmation</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f0fff4; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #38a169, #2f855a); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Password Reset Confirmation</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear Inspirahub User,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Your password for your Inspirahub account associated with <strong>{sanitized_email}</strong> has been successfully reset.
                        </p>
                        <div style="margin: 20px 0; padding: 15px; background-color: #f0fff4; border: 1px solid #c6f6d5; border-radius: 8px;">
                            <p style="font-size: 16px; line-height: 1.5; margin: 0; font-weight: 500;">Reset Token Used:</p>
                            <p style="font-size: 18px; font-weight: 700; color: #2f855a; margin: 5px 0 0;">{sanitized_token}</p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            You can now log in with your new password. If you did not initiate this change, please contact our support team immediately at 
                            <a href="mailto:{support_email}" style="color: #2f855a; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="https://www.inspirahub.com/login" style="display: inline-block; padding: 12px 24px; background-color: #2f855a | 
                            <a href="mailto:{support_email}" style="color: #2f855a; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} in response to your password reset request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Create and send the email
            msg = Message(
                email_subject,
                recipients=[sanitized_email],
                sender="intuitivers@gmail.com",
                reply_to=support_email
            )
            msg.body = email_body
            msg.html = html_body
            logger.debug(f"Attempting to send confirmation email to {sanitized_email}")
            mail.send(msg)
            logger.info(f"Sent password reset confirmation email to: {sanitized_email}")

        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_reset_password_success: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except psycopg2.Error as e:
            logger.error(f"Database error in process_reset_password_success: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_reset_password_success: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)



@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        email = request.form["email"]
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, username, user_verified FROM accounts WHERE email = %s", (email,))
                    user = cursor.fetchone()
                    if user:
                        account_id, username, user_verified = user
                        if not user_verified:
                            logger.debug(f"Unverified account for email: {email}")
                            flash(
                                "Your account is not verified. Please verify your account to reset your password.",
                                "error"
                            )
                            return redirect(url_for("reset_password"))
                        process_reset_password_emails.delay(account_id, username, email)
                        logger.info(f"Queued reset password task for email: {email}, account_id: {account_id}")
                        flash(
                            "Password reset instructions have been sent to your email. Reset and log in again",
                            "success"
                        )
                        return redirect(url_for("login"))
                    else:
                        logger.debug(f"No user found for email: {email}")
                        return render_template("auth/email_not_found.html")
        except psycopg2.Error as e:
            logger.error(f"Database error in reset_password: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("A database error occurred. Please try again.", "error")
            return redirect(url_for("reset_password"))
        except Exception as e:
            logger.error(f"Unexpected error in reset_password: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("reset_password"))
    return render_template("auth/reset_password_request.html")


@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password_token(token):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Delete expired tokens
                cursor.execute(
                    "DELETE FROM reset_tokens WHERE reset_password_token_expiration < %s",
                    (datetime.now(timezone.utc),)
                )
                cursor.execute(
                    "SELECT account_id, email, reset_password_token_expiration FROM reset_tokens WHERE reset_password_token = %s",
                    (token,)
                )
                token_data = cursor.fetchone()
                if token_data:
                    account_id, email, expiration_time = token_data
                    # Make expiration_time offset-aware (assume UTC)
                    expiration_time = expiration_time.replace(tzinfo=timezone.utc)
                    if expiration_time < datetime.now(timezone.utc):
                        cursor.execute(
                            "DELETE FROM reset_tokens WHERE reset_password_token = %s",
                            (token,)
                        )
                        conn.commit()
                        logger.warning(f"Expired reset token: {token} for email: {email}")
                        return render_template("auth/password_reset_link_expired.html")
                    if request.method == "POST":
                        new_password = request.form["password"]
                        process_reset_password_success.delay(account_id, email, new_password, token)
                        logger.info(f"Queued reset password success task for email: {email}, account_id: {account_id}")
                        flash(
                            "Your password is being reset. You will receive a confirmation email shortly.",
                            "success"
                        )
                        return redirect(url_for("login"))
                    return render_template("auth/reset_password.html")
                else:
                    logger.warning(f"Invalid or non-existent reset token: {token}")
                    return render_template("auth/password_reset_link_expired.html")

    except psycopg2.Error as e:
        logger.error(f"Database error in reset_password_token: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred. Please try again.", "error")
        return redirect(url_for("reset_password"))

    except Exception as e:
        logger.error(f"Unexpected error in reset_password_token: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("reset_password"))


def get_current_email(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT email FROM accounts WHERE id = %s", (user_id,))
                current_email = cursor.fetchone()
                if current_email:
                    email = current_email[0]
                    if not re.match(EMAIL_REGEX, email):
                        logger.error(f"Invalid email format in database for user_id {user_id}: {email}")
                        return None
                    logger.debug(f"Retrieved current email for user_id {user_id}: {email}")
                    return email
                return None
    except psycopg2.Error as e:
        logger.error(f"Database error in get_current_email: {e}")
        return None

def email_exists(email):
    if not re.match(EMAIL_REGEX, email):
        logger.error(f"Invalid email format in email_exists: {email}")
        return False
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM accounts WHERE email = %s", (email,))
                existing_user = cursor.fetchone()
                return bool(existing_user)
    except psycopg2.Error as e:
        logger.error(f"Database error in email_exists: {e}")
        return False

def mask_email(email):
    """
    Mask an email address for privacy (e.g., rehemamahozi@gmail.com -> r*****@***.com).
    """
    if not email or "@" not in email:
        return email
    local_part, domain = email.split("@")
    domain_name, domain_ext = domain.rsplit(".", 1)
    masked_local = f"{local_part[0]}*****" if len(local_part) > 1 else local_part
    masked_domain = "***"
    return f"{masked_local}@{masked_domain}.{domain_ext}"

@celery.task(bind=True, max_retries=3)
def process_email_update_emails(self, user_id, username, old_email, new_email, verification_token):
    with app.app_context():
        logger.debug(f"Task process_email_update_emails started with task_id {self.request.id} for user_id: {user_id}")
        try:
            logger.info(f"Starting email update task for user_id: {user_id}, new_email: {new_email}")
            
            # Sanitize inputs
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_old_email = bleach.clean(old_email, tags=[], strip=True)
            sanitized_new_email = bleach.clean(new_email, tags=[], strip=True)
            sanitized_token = bleach.clean(verification_token, tags=[], strip=True)

            # Mask the new email for the old email notification
            masked_new_email = mask_email(sanitized_new_email)
            logger.debug(f"Masked new_email: {masked_new_email} for old_email notification")

            # Email configuration
            server_address = "http://127.0.0.1:5000"
            verification_link = f"{server_address}/verify_new_email/{sanitized_token}"
            support_email = "support@inspirahub.com"

            # Send verification email to new email
            email_verification_subject = "Inspirahub: Verify Your New Email Address"
            email_verification_body = (
                f"Dear {sanitized_username},\n\n"
                f"Please verify your new email address by clicking the link below:\n"
                f"{verification_link}\n\n"
                f"This link will expire in 30 minutes. If you did not request this change, please contact support at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"{server_address}"
            )
            email_verification_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Email Verification</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #ebf8ff; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Email Verification</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            A request has been made to update the email address associated with your Inspirahub account.
                            Please verify your new email address <strong>{sanitized_new_email}</strong> by clicking the button below.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            This link will expire in <strong>30 minutes</strong>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="{verification_link}" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Verify New Email
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Alternatively, copy and paste this link into your browser: 
                            <a href="{verification_link}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{verification_link}</a>
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not request this change, please contact our support team at 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="{server_address}" style="color: #2b6cb0; text-decoration: none;">{server_address}</a> | 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_new_email} in response to your email update request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            msg = Message(
                email_verification_subject,
                recipients=[sanitized_new_email],
                sender=app.config["MAIL_DEFAULT_SENDER"],
                reply_to=support_email
            )
            msg.body = email_verification_body
            msg.html = email_verification_html
            logger.debug(f"Attempting to send verification email to {sanitized_new_email}")
            mail.send(msg)
            logger.info(f"Sent verification email to: {sanitized_new_email}")

            # Send notification email to old email with masked new email
            notification_subject = "Inspirahub: Email Address Update Request Notification"
            notification_body = (
                f"Dear {sanitized_username},\n\n"
                f"A request has been made to update your Inspirahub account email to {masked_new_email}.\n"
                f"If you did not initiate this change, please contact support immediately at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"{server_address}"
            )
            notification_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Email Update Notification</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #fff5f5; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #ed8936, #dd6b20); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Email Update Notification</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            A request has been made to update your Inspirahub account email to <strong>{masked_new_email}</strong>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not initiate this change, please contact our support team immediately at 
                            <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="mailto:{support_email}" style="display: inline-block; padding: 12px 24px; background-color: #dd6b20; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Contact Support
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #feebc8; padding: 15px; text-align: center; font-size: 12px; color: #7b341e;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="{server_address}" style="color: #dd6b20; text-decoration: none;">{server_address}</a> | 
                            <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_old_email} regarding an email update request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            msg = Message(
                notification_subject,
                recipients=[sanitized_old_email],
                sender=app.config["MAIL_DEFAULT_SENDER"],
                reply_to=support_email
            )
            msg.body = notification_body
            msg.html = notification_html
            logger.debug(f"Attempting to send notification email to {sanitized_old_email}")
            mail.send(msg)
            logger.info(f"Sent update notification to: {sanitized_old_email}")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error in process_email_update_emails for new_email: {sanitized_new_email}, old_email: {sanitized_old_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused in process_email_update_emails for new_email: {sanitized_new_email}, old_email: {sanitized_old_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_email_update_emails for new_email: {sanitized_new_email}, old_email: {sanitized_old_email}: {type(e).__name__}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except psycopg2.Error as e:
            logger.error(f"Database error in process_email_update_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_email_update_emails for new_email: {sanitized_new_email}, old_email: {sanitized_old_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
            

@celery.task(bind=True, max_retries=3)
def process_email_verification_emails(self, user_id, username, old_email, new_email):
    with app.app_context():
        try:
            logger.info(f"Starting email verification task for user_id: {user_id}, new_email: {new_email}, old_email: {old_email}, task_id: {self.request.id}")
            
            # Validate email formats
            if not re.match(EMAIL_REGEX, new_email):
                logger.error(f"Invalid new_email format: {new_email}")
                return
            if not re.match(EMAIL_REGEX, old_email):
                logger.error(f"Invalid old_email format: {old_email}")
                return

            # Sanitize inputs
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_new_email = bleach.clean(new_email, tags=[], strip=True)
            sanitized_old_email = bleach.clean(old_email, tags=[], strip=True)

            # Mask the new email for the old email notification
            masked_new_email = mask_email(sanitized_new_email)
            logger.debug(f"Masked new_email: {masked_new_email} for old_email notification")

            # Email configuration
            server_address = "http://127.0.0.1:5000"
            support_email = "support@inspirahub.com"

            # Send confirmation email to new email
            confirmation_subject = "Inspirahub: Email Verification Complete"
            confirmation_body = (
                f"Dear {sanitized_username},\n\n"
                f"Your new email address ({sanitized_new_email}) has been successfully verified.\n"
                f"You can now log in to Inspirahub with your new email.\n\n"
                f"If you did not initiate this change, please contact support at {support_email}.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"{server_address}"
            )
            confirmation_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Email Verification Complete</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f0fff4; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #38a169, #2f855a); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Email Verification Complete</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Your new email address <strong>{sanitized_new_email}</strong> has been successfully verified.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            You can now log in to Inspirahub with your new email.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="{server_address}/login" style="display: inline-block; padding: 12px 24px; background-color: #2f855a; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Log In Now
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not initiate this change, please contact our support team immediately at 
                            <a href="mailto:{support_email}" style="color: #2f855a; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #c6f6d5; padding: 15px; text-align: center; font-size: 12px; color: #2f855a;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="{server_address}" style="color: #2f855a; text-decoration: none;">{server_address}</a> | 
                            <a href="mailto:{support_email}" style="color: #2f855a; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_new_email} regarding your email verification.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            msg = Message(
                confirmation_subject,
                recipients=[sanitized_new_email],
                sender=app.config["MAIL_DEFAULT_SENDER"],
                reply_to=support_email
            )
            msg.body = confirmation_body
            msg.html = confirmation_html
            logger.debug(f"Attempting to send confirmation email to {sanitized_new_email}")
            mail.send(msg)
            logger.info(f"Sent verification confirmation to: {sanitized_new_email}")

            # Send final notification to old email (if different) with masked new email
            if sanitized_old_email != sanitized_new_email:
                final_notification_subject = "Inspirahub: Email Address Update Confirmation"
                final_notification_body = (
                    f"Dear {sanitized_username},\n\n"
                    f"Your Inspirahub account email has been changed to {masked_new_email}.\n"
                    f"If you did not initiate this change, please contact support immediately at {support_email}.\n"
                    f"To recover your account, provide your username ({sanitized_username}) or other identifying information.\n\n"
                    f"Best regards,\n"
                    f"The Inspirahub Team\n"
                    f"Inspirahub - Connecting Communities\n"
                    f"{server_address}"
                )
                final_notification_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Inspirahub Email Update Confirmation</title>
                </head>
                <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #fff5f5; padding: 20px; margin: 0;">
                    <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                        <div style="background: linear-gradient(90deg, #ed8936, #dd6b20); color: #ffffff; padding: 25px; text-align: center;">
                            <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                            <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Email Update Confirmation</p>
                        </div>
                        <div style="padding: 30px;">
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                Your Inspirahub account email has been changed to <strong>{masked_new_email}</strong>.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                If you did not initiate this change, please contact our support team immediately at 
                                <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none; font-weight: 500;">{support_email}</a>.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                To recover your account, provide your username (<strong>{sanitized_username}</strong>) or other identifying information.
                            </p>
                            <div style="text-align: center; margin: 20px 0;">
                                <a href="mailto:{support_email}" style="display: inline-block; padding: 12px 24px; background-color: #dd6b20; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                    Contact Support
                                </a>
                            </div>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                        </div>
                        <div style="background-color: #feebc8; padding: 15px; text-align: center; font-size: 12px; color: #7b341e;">
                            <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                            <p style="margin: 5px 0 0;">
                                <a href="{server_address}" style="color: #dd6b20; text-decoration: none;">{server_address}</a> | 
                                <a href="mailto:{support_email}" style="color: #dd6b20; text-decoration: none;">Contact Support</a>
                            </p>
                            <p style="margin: 5px 0 0; opacity: 0.7;">
                                This message was sent to {sanitized_old_email} regarding an email update.
                            </p>
                        </div>
                    </div>
                </body>
                </html>
                """
                msg = Message(
                    final_notification_subject,
                    recipients=[sanitized_old_email],
                    sender=app.config["MAIL_DEFAULT_SENDER"],
                    reply_to=support_email
                )
                msg.body = final_notification_body
                msg.html = final_notification_html
                logger.debug(f"Attempting to send final notification to {sanitized_old_email}")
                mail.send(msg)
                logger.info(f"Sent final update notification to: {sanitized_old_email}")
            else:
                logger.info(f"Old email same as new email ({sanitized_old_email}), skipping final notification")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error in process_email_verification_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused in process_email_verification_emails: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_email_verification_emails: {type(e).__name__}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except psycopg2.Error as e:
            logger.error(f"Database error in process_email_verification_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_email_verification_emails: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)


@celery.task(bind=True, max_retries=3)
def cleanup_expired_tokens(self):
    with app.app_context():
        try:
            logger.info("Starting cleanup of expired tokens")
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM tokens WHERE verification_token_expiration < %s",
                        (datetime.now(timezone.utc),)
                    )
                    deleted_count = cursor.rowcount
                    conn.commit()
                    logger.info(f"Deleted {deleted_count} expired tokens")
        except psycopg2.Error as e:
            logger.error(f"Database error in cleanup_expired_tokens: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in cleanup_expired_tokens: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)


@app.route("/update_email", methods=["GET", "POST"])
def update_email():
    if "user_id" not in session or "username" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_email = request.form["new_email"]
        old_email_input = request.form["old_email"]
        username = session["username"]
        user_id = session["user_id"]

        # Sanitize inputs
        sanitized_new_email = bleach.clean(new_email, tags=[], strip=True)
        sanitized_old_email_input = bleach.clean(old_email_input, tags=[], strip=True)
        sanitized_username = bleach.clean(username.title(), tags=[], strip=True)

        # Validate old email
        old_email = get_current_email(user_id)
        if not old_email:
            logger.error(f"Unable to retrieve current email for user_id {user_id}")
            flash("Unable to retrieve current email. Please try again.", "error")
            return redirect(url_for("update_email"))
        logger.debug(f"Old email for user_id {user_id}: {old_email}")
        if sanitized_old_email_input != old_email:
            logger.warning(f"Provided old email {sanitized_old_email_input} does not match current email {old_email} for user_id {user_id}")
            flash("The provided current email does not match the email associated with your account.", "error")
            return redirect(url_for("update_email"))

        # Check if new email is valid and not in use
        if not re.match(EMAIL_REGEX, sanitized_new_email):
            logger.warning(f"Invalid new email format: {sanitized_new_email}")
            flash("Invalid new email format.", "error")
            return redirect(url_for("update_email"))
        if email_exists(sanitized_new_email):
            logger.warning(f"New email {sanitized_new_email} already in use for user_id {user_id}")
            flash("Email address is already in use.", "error")
            return redirect(url_for("update_email"))

        # Generate and store verification token, set user_verified to False
        verification_token = generate_verification_token(length=32)
        verification_sent_time = datetime.now(timezone.utc)
        verification_token_expiration = verification_sent_time + timedelta(minutes=30)
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # Delete existing tokens for this user
                    cursor.execute("DELETE FROM tokens WHERE account_id = %s", (user_id,))
                    # Store new token
                    cursor.execute(
                        "INSERT INTO tokens (account_id, username, email, verification_token, verification_sent_time, "
                        "verification_token_expiration) VALUES (%s, %s, %s, %s, %s, %s)",
                        (user_id, sanitized_username, sanitized_new_email, verification_token, verification_sent_time, verification_token_expiration),
                    )
                    # Set user_verified to False
                    cursor.execute(
                        "UPDATE accounts SET user_verified = FALSE WHERE id = %s",
                        (user_id,)
                    )
                    conn.commit()
                    logger.info(f"Stored verification token and set user_verified=False for user_id {user_id}")
        except psycopg2.Error as e:
            logger.error(f"Database error in update_email for user_id {user_id}: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("update_email"))

        # Queue Celery task for initial emails
        try:
            task = process_email_update_emails.delay(user_id, sanitized_username, old_email, sanitized_new_email, verification_token)
            logger.info(f"Queued email update task for user_id {user_id} with task_id {task.id}")
        except Exception as e:
            logger.error(f"Error queuing email update task for user_id {user_id}: {str(e)}", exc_info=True)
            flash("Failed to queue email tasks. Please try again.", "error")
            return redirect(url_for("update_email"))

        # Log out user
        session.clear()
        flash("A verification email has been sent to your new email address. Please verify it to continue.", "success")
        return redirect(url_for("login"))

    return render_template("account/update_email_form.html")

@app.route("/verify_new_email/<token>", methods=["GET"])
def verify_new_email(token):
    # Sanitize token
    sanitized_token = bleach.clean(token, tags=[], strip=True)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Explicitly select the expected columns to avoid unpacking errors
                cursor.execute(
                    "SELECT account_id, username, email, verification_token, verification_sent_time, verification_token_expiration "
                    "FROM tokens WHERE verification_token = %s",
                    (sanitized_token,)
                )
                token_data = cursor.fetchone()
    except psycopg2.Error as e:
        logger.error(f"Database error querying token {sanitized_token}: {str(e)}", exc_info=True)
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("login"))

    if token_data:
        # Unpack exactly 6 values
        account_id, username, new_email, verification_token, verification_sent_time, verification_token_expiration = token_data
        # Make verification_sent_time offset-aware (assume UTC)
        verification_sent_time = verification_sent_time.replace(tzinfo=timezone.utc)
        current_time = datetime.now(timezone.utc)
        time_difference = (current_time - verification_sent_time).total_seconds() / 60

        if time_difference > 30:
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("DELETE FROM tokens WHERE verification_token = %s", (sanitized_token,))
                        conn.commit()
                        logger.info(f"Deleted expired token for token {sanitized_token}")
            except psycopg2.Error as e:
                logger.error(f"Database error deleting expired token {sanitized_token}: {str(e)}", exc_info=True)
                flash("Database error occurred. Please try again.", "error")
                return redirect(url_for("update_email"))
            flash("Verification link has expired. Please request a new verification email.", "error")
            return redirect(url_for("update_email"))

        # Get the current (old) email before updating
        old_email = get_current_email(account_id)
        if not old_email:
            logger.error(f"Failed to retrieve old email for user_id {account_id}")
            flash("Unable to retrieve current email. Please try again.", "error")
            return redirect(url_for("login"))

        # Sanitize username and new_email
        sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
        sanitized_new_email = bleach.clean(new_email, tags=[], strip=True)

        # Update email and set user_verified to True
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE accounts SET email = %s, user_verified = TRUE WHERE id = %s",
                        (sanitized_new_email, account_id)
                    )
                    cursor.execute("DELETE FROM tokens WHERE verification_token = %s", (sanitized_token,))
                    conn.commit()
                    logger.info(f"Updated email to {sanitized_new_email} and verified for user_id {account_id}")
        except psycopg2.Error as e:
            logger.error(f"Database error updating email for user_id {account_id}: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("login"))

        # Queue Celery task for verification emails
        try:
            task = process_email_verification_emails.delay(account_id, sanitized_username, old_email, sanitized_new_email)
            logger.info(f"Queued verification email task for user_id {account_id} with task_id {task.id}")
        except Exception as e:
            logger.error(f"Error queuing verification email task for user_id {account_id}: {str(e)}", exc_info=True)
            flash("Failed to queue confirmation emails. Please contact support.", "warning")

        # Ensure user is logged out
        session.clear()
        flash("Your new email address has been verified. Please log in to continue.", "success")
        return redirect(url_for("login"))

    logger.warning(f"Invalid verification token: {sanitized_token}")
    flash("Invalid verification link. Please request a new verification email.", "error")
    return redirect(url_for("update_email"))


# Function to check allowed file extensions
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/upload_profile_image", methods=["GET", "POST"])
def upload_profile_image():
    if "user_id" not in session:
        logging.debug("No user_id in session, redirecting to login")
        flash("You need to login first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    if request.method == "POST":
        # Check if file is in request
        if "file" not in request.files:
            logging.debug("No file part in request")
            flash("No file selected.", "error")
            return redirect(url_for("profile", username=session["username"]))

        file = request.files["file"]
        if file.filename == "":
            logging.debug("No file selected")
            flash("No file selected.", "error")
            return redirect(url_for("profile", username=session["username"]))

        if file and allowed_file(file.filename):
            try:
                # Ensure the Uploads directory exists
                if not os.path.exists(app.config["UPLOAD_FOLDER"]):
                    os.makedirs(app.config["UPLOAD_FOLDER"])
                    logging.info(f"Created directory: {app.config['UPLOAD_FOLDER']}")

                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                file.save(file_path)
                logging.debug(f"File saved: {file_path}")

                # Update profile_picture in accounts
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE accounts SET profile_picture = %s WHERE id = %s",
                            (filename, user_id),
                        )
                    conn.commit()
                logging.debug(f"Updated profile picture for user_id: {user_id}")

                flash("Profile image uploaded successfully.", "success")
                return redirect(url_for("profile", username=session["username"]))

            except psycopg2.Error as e:
                logging.error(f"Database error in upload_profile_image: {e}")
                flash("Database error occurred. Please try again.", "error")
                return redirect(url_for("profile", username=session["username"]))
            except Exception as e:
                logging.error(f"Unexpected error in upload_profile_image: {e}", exc_info=True)
                flash("An unexpected error occurred. Please try again.", "error")
                return redirect(url_for("profile", username=session["username"]))
        else:
            logging.debug(f"Invalid file type: {file.filename}")
            flash("Invalid file type. Allowed: png, jpg, jpeg.", "error")
            return redirect(url_for("profile", username=session["username"]))

    # Render upload form for GET request
    return render_template("account/upload_profile_image.html")


@app.route("/Uploads/<filename>")
def uploaded_file(filename):
    try:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        if not os.path.exists(file_path):
            logging.error(f"File not found: {file_path}")
            flash("Requested file not found.", "error")
            return redirect(url_for("profile", username=session.get("username", "")))
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)
    except Exception as e:
        logging.error(f"Error serving file {filename}: {e}", exc_info=True)
        flash("Error serving file. Please try again.", "error")
        return redirect(url_for("profile", username=session.get("username", "")))


# Function to check password strength (matching register route requirements)
def is_strong_password(password):
    if len(password) < 12:
        return False
    if not re.search(r"[A-Z]", password):
        return False
    if not re.search(r"[a-z]", password):
        return False
    if not re.search(r"[0-9]", password):
        return False
    if len(re.findall(r'[!@#$%^&*(),.?":{}|<>]', password)) < 2:
        return False
    if re.search(r'(.)\1{2,}', password):
        return False
    return True

# Celery task for sending password change email
@celery.task(bind=True, ignore_result=True, max_retries=3, retry_backoff=True)
def send_password_change_email_task(self, email, username):
    with app.app_context():
        try:
            sender_email = "intuitivers@gmail.com"
            subject = "Password Changed Confirmation"
            recipients = [email]

            message_body = (
                f"Dear {username},\n\n"
                f"We wanted to inform you that your password has been successfully changed. This email serves as confirmation "
                f"of the recent update. If you authorized this change, you can disregard this message.\n\n"
                f"However, if you did not initiate this password change, it could indicate a security concern. We urge you to "
                f"immediately contact our support team for further assistance. Your security is our top priority.\n\n"
                f"Thank you for your attention and cooperation.\n\n"
                f"Best regards,\n"
                f"The Intuitivers Team"
            )

            msg = Message(subject, sender_email=sender_email, recipients=recipients)
            msg.body = message_body

            mail.send(msg)
            current_app.logger.info(f"Password change email sent to {email}")
        except Exception as e:
            current_app.logger.error(f"Failed to send email to {email}: {str(e)}")
            raise self.retry(exc=e)

@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if "user_id" in session:
        user_id = session["user_id"]
        username = session["username"]
        
        # Get database connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            if request.method == "POST":
                current_password = request.form["current_password"]
                new_password = request.form["new_password"]

                # Fetch the current hashed password and email from the database
                cursor.execute(
                    "SELECT password, email FROM accounts WHERE id = %s", (
                        user_id,)
                )
                result = cursor.fetchone()
                if not result:
                    flash("User not found.", "error")
                    return redirect(url_for("login"))

                stored_password, user_email = result[0], result[1]

                # Verify the current password provided by the user
                if check_password_hash(stored_password, current_password):
                    # Check if the new password meets the strength requirements
                    if is_strong_password(new_password):
                        # Hash the new password before updating it in the database
                        hashed_password = generate_password_hash(
                            new_password, method="pbkdf2:sha256", salt_length=8
                        )

                        # Update the user's password in the database
                        cursor.execute(
                            "UPDATE accounts SET password = %s WHERE id = %s",
                            (hashed_password, user_id),
                        )

                        # Commit the changes to the database
                        conn.commit()

                        # Clear all session data (log out user from all sessions)
                        session.clear()

                        # Send email notification to the user in the background
                        send_password_change_email_task.delay(user_email, username)

                        flash(
                            "Password changed successfully. "
                            "You have been logged out from all sessions except the current one.",
                            "success",
                        )
                        return redirect(url_for("login"))
                    else:
                        flash(
                            "Password must be at least 12 characters long, contain at least one uppercase letter, "
                            "one lowercase letter, one digit, two special characters (e.g., !@#$%), and no repetitive characters (e.g., aaa, 111).",
                            "error",
                        )
                else:
                    flash("Incorrect current password. Please try again.", "error")

            return render_template("account/change_password.html")
        finally:
            # Ensure cursor and connection are closed
            cursor.close()
            conn.close()
    else:
        flash("You are not logged in. Please log in to change your password.", "error")
        return redirect(url_for("login"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        flash("You need to be logged in to access the settings page.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    username = session["username"]
    print(
        f"User ID: {user_id} | Username: {username} accessing settings page.")

    # rendering the settings page
    return render_template("account/settings.html")


@app.route("/account", methods=["GET", "POST"])
def account():
    if "user_id" not in session:
        flash("You need to be logged in to access the account page.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    username = session["username"]
    print(f"User ID: {user_id} | Username: {username} accessing account page.")

    # rendering the account page
    return render_template("account/account.html")


@app.route("/privacy")
def privacy():
    if "user_id" not in session:
        flash("You need to be logged in to access the privacy page.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    username = session["username"]
    print(f"User ID: {user_id} | Username: {username} accessing privacy page.")

    #  rendering the privacy page
    return render_template("insipirahub/privacy.html")


# TFA-related functions
def check_tfa_status(email):
    email = str(email)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT "tfa", id, username FROM accounts WHERE email = %s',
                (email,),
            )
            user_data = cursor.fetchone()
    if user_data:
        return user_data[0], user_data[1], user_data[2]
    return None, None, None


@celery.task(bind=True, max_retries=3)
def process_tfa_update(self, email, status, username):
    with app.app_context():
        try:
            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_status = bleach.clean(status, tags=[], strip=True)

            logger.info(f"Starting TFA update task for user: {sanitized_email}, status: {sanitized_status}")

            # Validate email format
            if not re.match(EMAIL_REGEX, sanitized_email):
                logger.error(f"Invalid email format: {sanitized_email}")
                return

            # Update TFA status in database
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'UPDATE accounts SET "tfa" = %s WHERE email = %s',
                        (sanitized_status, sanitized_email)
                    )
                    conn.commit()
                    logger.info(f"Updated TFA status to {sanitized_status} for email: {sanitized_email}")

            # Email configuration
            support_email = "support@inspirahub.com"
            sender_email = app.config.get("MAIL_DEFAULT_SENDER", support_email)
            subject = "Inspirahub: Two-Factor Authentication Update"
            recipient_email = [sanitized_email]

            # Define email content based on TFA status
            if sanitized_status == "T":
                plain_text_body = (
                    f"Dear {sanitized_username},\n\n"
                    f"We received a request to activate Two-Factor Authentication (TFA) for your Inspirahub account.\n"
                    f"We're pleased to inform you that TFA has been successfully activated.\n\n"
                    f"Your account is now protected with an additional layer of security. Each time you log in, "
                    f"you'll need to provide a verification code sent to your email.\n\n"
                    f"If you did not request this change, please contact support at {support_email}.\n"
                    f"Thank you for prioritizing your account's security!\n\n"
                    f"Best regards,\n"
                    f"The Inspirahub Team\n"
                    f"Inspirahub - Connecting Communities\n"
                    f"https://www.inspirahub.com"
                )
                html_body = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Inspirahub TFA Activation</title>
                </head>
                <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f9f9f9; padding: 20px; margin: 0;">
                    <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                        <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                            <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                            <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Two-Factor Authentication Activated</p>
                        </div>
                        <div style="padding: 30px;">
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                We're pleased to inform you that <strong>Two-Factor Authentication (TFA)</strong> has been successfully activated for your Inspirahub account associated with <strong>{sanitized_email}</strong>.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                Your account is now protected with an additional layer of security. Each time you log in, you'll need to provide a verification code sent to your email.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                If you did not request this change, please contact our support team immediately at 
                                <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                            </p>
                            <div style="text-align: center; margin: 20px 0;">
                                <a href="http://localhost:5000" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                    Learn More About TFA
                                </a>
                            </div>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0;">Thank you for prioritizing your account's security!</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">Best regards,</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                        </div>
                        <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                            <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                            <p style="margin: 5px 0 0;">
                                <a href="http://localhost:5000" style="color: #2b6cb0; text-decoration: none;">localhost</a> | 
                                <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                            </p>
                            <p style="margin: 5px 0 0; opacity: 0.7;">
                                This message was sent to {sanitized_email} regarding your TFA activation.
                            </p>
                        </div>
                    </div>
                </body>
                </html>
                """
            else:
                plain_text_body = (
                    f"Dear {sanitized_username},\n\n"
                    f"We received a request to deactivate Two-Factor Authentication (TFA) for your Inspirahub account.\n"
                    f"We're confirming that TFA has been successfully deactivated.\n\n"
                    f"Your account no longer requires a verification code during login.\n"
                    f"If you did not request this change, please contact support at {support_email}.\n"
                    f"Thank you for using Inspirahub!\n\n"
                    f"Best regards,\n"
                    f"The Inspirahub Team\n"
                    f"Inspirahub - Connecting Communities\n"
                    f"http://localhost:5000"
                )
                html_body = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Inspirahub TFA Deactivation</title>
                </head>
                <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f9f9f9; padding: 20px; margin: 0;">
                    <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                        <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                            <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                            <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Two-Factor Authentication Deactivated</p>
                        </div>
                        <div style="padding: 30px;">
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                We're confirming that <strong>Two-Factor Authentication (TFA)</strong> has been successfully deactivated for your Inspirahub account associated with <strong>{sanitized_email}</strong>.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                Your account no longer requires a verification code during login.
                            </p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                                If you did not request this change, please contact our support team immediately at 
                                <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                            </p>
                            <div style="text-align: center; margin: 20px 0;">
                                <a href="http://localhost:5000" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                    Learn More About TFA
                                </a>
                            </div>
                            <p style="font-size: 16px; line-height: 1.6; margin: 0;">Thank you for using Inspirahub!</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">Best regards,</p>
                            <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                        </div>
                        <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                            <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                            <p style="margin: 5px 0 0;">
                                <a href="http://localhost:5000" style="color: #2b6cb0; text-decoration: none;">localhost</a> | 
                                <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                            </p>
                            <p style="margin: 5px 0 0; opacity: 0.7;">
                                This message was sent to {sanitized_email} regarding your TFA deactivation.
                            </p>
                        </div>
                    </div>
                </body>
                </html>
                """

            # Create and send email
            msg = Message(
                subject,
                sender=sender_email,
                recipients=recipient_email,
                reply_to=support_email
            )
            msg.body = plain_text_body
            msg.html = html_body
            mail.send(msg)
            logger.info(f"Sent TFA {sanitized_status} email to: {sanitized_email}")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error in process_tfa_update for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused in process_tfa_update for {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in process_tfa_update for {sanitized_email}: {type(e).__name__}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except psycopg2.Error as e:
            logger.error(f"Database error in process_tfa_update for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in process_tfa_update for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)


@app.route("/activate_tfa", methods=["GET", "POST"])
def activate_tfa():
    if "user_id" not in session:
        flash("You need to be logged in to manage TFA.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    stored_email = session["email"]
    username = session["username"]

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT "tfa" FROM accounts WHERE id = %s', (user_id,)
            )
            current_tfa_status = cursor.fetchone()[0]

    logger.debug(f"Logged-in user ID: {user_id}, username: {username}, email: {stored_email}, current TFA status: {current_tfa_status}")

    if request.method == "POST":
        entered_email = request.form["email"]
        entered_email = str(entered_email)
        user_input = request.form["user_input"].lower()

        logger.debug(f"Entered email: {entered_email}, user input: {user_input}")

        if entered_email == stored_email:
            if user_input == "deactivate" and current_tfa_status == "T":
                process_tfa_update.delay(stored_email, "F", username)
                flash("Two Factor Authentication deactivation requested. Check your email for confirmation.", "success")
                return redirect(url_for("settings"))
            elif user_input == "deactivate" and current_tfa_status == "F":
                flash("Two Factor Authentication is not activated, so it cannot be deactivated.", "info")
            elif user_input == "activate" and current_tfa_status == "T":
                flash("Two Factor Authentication is already activated. Enter 'deactivate' to disable it.", "info")
            elif user_input == "activate" and current_tfa_status == "F":
                process_tfa_update.delay(stored_email, "T", username)
                flash("Two Factor Authentication activation requested. Check your email for confirmation.", "success")
                return redirect(url_for("settings"))
            else:
                flash("Invalid input or TFA status. Please try again.", "error")
        else:
            flash("The entered email address does not match your account email.", "error")
        return redirect(url_for("activate_tfa"))

    return render_template("auth/tfa/activate_tfa.html", current_tfa_status=current_tfa_status)


# Validation functions
def is_valid_email(email):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM accounts WHERE email = %s", (email,)
                )
                count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        app.logger.error(f"Error validating email: {str(e)}")
        return False

def is_valid_username(username):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM accounts WHERE username = %s", (username,)
                )
                count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        app.logger.error(f"Error validating username: {str(e)}")
        return False

def is_valid_password(email, password):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT password FROM accounts WHERE email = %s", (email,)
                )
                stored_password = cursor.fetchone()
        return stored_password and check_password_hash(stored_password[0], password)
    except Exception as e:
        app.logger.error(f"Error validating password: {str(e)}")
        return False

def is_valid_security_pin(email, security_pin):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT security_pin FROM accounts WHERE email = %s", (email,)
                )
                stored_security_pin = cursor.fetchone()
        return stored_security_pin and stored_security_pin[0] == security_pin
    except Exception as e:
        app.logger.error(f"Error validating security pin: {str(e)}")
        return False


@celery.task(bind=True, max_retries=3, retry_backoff=True)
def send_account_deletion_confirmation_non_tfa_email_task(self, email, username):
    with app.app_context():
        try:
            sender_email = app.config["MAIL_DEFAULT_SENDER"]
            support_email = "support@inspirahub.com"
            subject = "Inspirahub: Account Deletion Confirmation"

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)

            # Plain-text body
            plain_text_body = (
                f"Dear {sanitized_username},\n\n"
                f"Your Inspirahub account associated with {sanitized_email} has been successfully deleted.\n\n"
                f"If you did not request this deletion, please contact support at {support_email}.\n"
                f"Thank you for being part of our community. We hope to see you again!\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"http://localhost:5000"
            )

            # HTML body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Account Deletion Confirmation</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f9f9f9; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Account Deletion Confirmation</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Your Inspirahub account associated with <strong>{sanitized_email}</strong> has been successfully deleted.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not request this deletion, please contact our support team at 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Thank you for being part of our community. We’re sorry to see you go! We’d love to hear your feedback to improve our services.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="http://localhost:5000" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Share Your Feedback
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="http://localhost:5000" style="color: #2b6cb0; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} regarding your account deletion.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Create and send the email
            msg = Message(
                subject,
                sender=sender_email,
                recipients=[sanitized_email],
                reply_to=support_email
            )
            msg.body = plain_text_body
            msg.html = html_body
            mail.send(msg)
            logger.info(f"Non-TFA account deletion confirmation email sent to {sanitized_email}")
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused for {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error for {sanitized_email}: {str(e)}", exc_info=True)
            return


@celery.task(bind=True, max_retries=3, retry_backoff=True)
def send_tfa_deletion_token_email_task(self, email, token, username):
    with app.app_context():
        try:
            sender_email = app.config["MAIL_DEFAULT_SENDER"]
            support_email = "support@inspirahub.com"
            subject = "Inspirahub: Account Deletion Verification Token"

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_token = bleach.clean(token, tags=[], strip=True)

            # Plain-text body
            plain_text_body = (
                f"Dear {sanitized_username},\n\n"
                f"WARNING: We have received a request to DELETE your Inspirahub account associated with {sanitized_email}.\n\n"
                f"To confirm this action, please enter the following verification token within the next 2 minutes:\n"
                f"Token: {sanitized_token}\n\n"
                f"If you did not initiate this request, ignore this email or contact support at {support_email}.\n\n"
                f"Thank you,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"http://localhost:5000"
            )

            # HTML body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Account Deletion Verification</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #fff5f5; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #c53030, #742a2a); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Account Deletion Verification</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px; color: #c53030; font-weight: 600;">
                            WARNING: We have received a request to <strong>delete</strong> your Inspirahub account associated with <strong>{sanitized_email}</strong>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            To confirm this action, please enter the following verification token within the next <strong>2 minutes</strong>:
                        </p>
                        <div style="text-align: center; margin: 20px 0; padding: 15px; background-color: #fff5f5; border: 2px solid #feb2b2; border-radius: 8px;">
                            <p style="font-size: 24px; font-weight: 700; color: #c53030; margin: 0; letter-spacing: 2px;">{sanitized_token}</p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Enter this token on the verification page to complete the deletion process.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not initiate this request, <strong>ignore this email</strong> or contact our support team at 
                            <a href="mailto:{support_email}" style="color: #742a2a; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="mailto:{support_email}" style="display: inline-block; padding: 12px 24px; background-color: #742a2a; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Contact Support
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Thank you,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #fed7d7; padding: 15px; text-align: center; font-size: 12px; color: #742a2a;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="https://www.inspirahub.com" style="color: #742a2a; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #742a2a; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} regarding your account deletion request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Create and send the email
            msg = Message(
                subject,
                sender=sender_email,
                recipients=[sanitized_email],
                reply_to=support_email
            )
            msg.body = plain_text_body
            msg.html = html_body
            mail.send(msg)
            logger.info(f"TFA deletion token email sent to {sanitized_email}")
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused for {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error for {sanitized_email}: {str(e)}", exc_info=True)
            return


@celery.task(bind=True, max_retries=3, retry_backoff=True)
def send_account_deletion_confirmation_email_task(self, email, username, token=None):
    with app.app_context():
        try:
            sender_email = app.config["MAIL_DEFAULT_SENDER"]
            support_email = "support@inspirahub.com"
            subject = "Inspirahub: Account Deletion Confirmation"

            # Sanitize inputs
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_username = bleach.clean(username.title(), tags=[], strip=True)
            sanitized_token = bleach.clean(token, tags=[], strip=True) if token else "Not provided"

            # Plain-text body
            plain_text_body = (
                f"Dear {sanitized_username},\n\n"
                f"Your Inspirahub account associated with {sanitized_email} has been successfully deleted.\n"
                f"Verification Token Used: {sanitized_token}\n\n"
                f"If you did not initiate this action, please contact support at {support_email}.\n"
                f"Thank you for being part of our community. We hope to see you again!\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"https://www.inspirahub.com"
            )

            # HTML body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Account Deletion Confirmation</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f9f9f9; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Account Deletion Confirmation</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_username},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Your Inspirahub account associated with <strong>{sanitized_email}</strong> has been successfully deleted.
                        </p>
                        <div style="margin: 20px 0; padding: 15px; background-color: #f7fafc; border: 1px solid #e2e8f0; border-radius: 8px;">
                            <p style="font-size: 16px; line-height: 1.5; margin: 0; font-weight: 500;">Verification Token Used:</p>
                            <p style="font-size: 18px; font-weight: 700; color: #2b6cb0; margin: 5px 0 0;">{sanitized_token}</p>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            If you did not initiate this action, please contact our support team at 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">{support_email}</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Thank you for being part of our community. We’re sorry to see you go! We’d love to hear your feedback to improve our services.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="https://www.inspirahub.com/feedback" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Share Your Feedback
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="https://www.inspirahub.com" style="color: #2b6cb0; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} regarding your account deletion.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Create and send the email
            msg = Message(
                subject,
                sender=sender_email,
                recipients=[sanitized_email],
                reply_to=support_email
            )
            msg.body = plain_text_body
            msg.html = html_body
            mail.send(msg)
            logger.info(f"Account deletion confirmation email sent to {sanitized_email}")
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused for {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error for {sanitized_email}: {str(e)}", exc_info=True)
            return


# Account deletion routes
@app.route("/delete_account", methods=["GET", "POST"])
def delete_account():
    if "user_id" not in session:
        flash("You need to be logged in to delete your account.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT "tfa", email, username, password, security_pin FROM accounts WHERE id = %s',
                (user_id,),
            )
            user_data = cursor.fetchone()

    if not user_data:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    current_tfa_status = user_data[0]
    stored_email = user_data[1]
    stored_username = user_data[2]
    stored_password = user_data[3]
    stored_security_pin = user_data[4]

    app.logger.debug(f"User ID: {user_id}, TFA: {current_tfa_status}, Email: {stored_email}, Username: {stored_username}")

    if current_tfa_status == "T" and request.method == "POST":
        entered_email = request.form.get("email")
        entered_username = request.form.get("username")
        entered_password = request.form.get("password")
        entered_security_pin = request.form.get("security_pin")
        deletion_reasons = request.form.getlist("deletion_reason[]")

        if (
            entered_email == stored_email
            and entered_username == stored_username
            and is_valid_password(entered_email, entered_password)
            and entered_security_pin == stored_security_pin
        ):
            two_fa_token = generate_token()
            app.logger.debug(f"TFA Token: {two_fa_token}, Username: {stored_username}, Email: {stored_email}")

            session["email"] = stored_email
            session["username"] = stored_username
            session["deletion_reason"] = ", ".join(deletion_reasons) if deletion_reasons else "No reason provided"

            send_tfa_deletion_token_email_task.delay(stored_email, two_fa_token, stored_username)
            session["verification_token"] = two_fa_token
            flash("A TFA token has been sent to your email. Please check to confirm deletion.", "info")
            return render_template("auth/tfa/tfa_deletion_verification.html")
        else:
            flash("Invalid email, username, password, or security pin.", "error")
            return redirect(url_for("delete_account"))

    elif current_tfa_status == "F" and request.method == "POST":
        entered_email = request.form.get("email")
        entered_username = request.form.get("username")
        entered_password = request.form.get("password")
        entered_security_pin = request.form.get("security_pin")
        deletion_reasons = request.form.getlist("deletion_reason[]")

        if (
            entered_email == stored_email
            and entered_username == stored_username
            and is_valid_password(entered_email, entered_password)
            and entered_security_pin == stored_security_pin
        ):
            deletion_reason = ", ".join(deletion_reasons) if deletion_reasons else "No reason provided"
            deletion_date = date.today()

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO deleted_accounts (email, username, first_name, last_name, country, day, month, year, deleted_date, deletion_reason) "
                        "SELECT email, username, first_name, last_name, country, day, month, year, %s, %s FROM accounts WHERE id = %s",
                        (deletion_date, deletion_reason, user_id),
                    )
                    cursor.execute(
                        "DELETE FROM accounts WHERE id = %s", (user_id,)
                    )
                    conn.commit()

            send_account_deletion_confirmation_non_tfa_email_task.delay(stored_email, stored_username)
            session.clear()
            flash("Your account has been deleted successfully.", "success")
            return render_template("account/account_deleted_success.html")
        else:
            flash("Invalid email, username, password, or security pin.", "error")
            return redirect(url_for("delete_account"))
    return render_template("account/confirm_delete_account.html")


@app.route("/verify_tfa_deletion", methods=["POST"])
def verify_tfa_deletion():
    entered_token = request.form["verification_code"]
    stored_token = session.get("verification_token")

    if stored_token and entered_token == stored_token:
        user_id = session["user_id"]
        user_email = session.get("email")
        username = session.get("username")
        deletion_reason = session.get("deletion_reason", "No reason provided")

        if not user_email or not username:
            flash("Session data missing. Please try again.", "error")
            return redirect(url_for("delete_account"))

        deletion_date = date.today()

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO deleted_accounts (email, username, first_name, last_name, country, day, month, year, deleted_date, deletion_reason) "
                    "SELECT email, username, first_name, last_name, country, day, month, year, %s, %s FROM accounts WHERE id = %s",
                    (deletion_date, deletion_reason, user_id),
                )
                cursor.execute(
                    "DELETE FROM accounts WHERE id = %s", (user_id,)
                )
                conn.commit()

        # Pass the verification token to the email task
        send_account_deletion_confirmation_email_task.delay(user_email, username, stored_token)
        session.clear()
        flash("Your account has been deleted successfully.", "success")
        return render_template("account/account_deleted_success.html")
    else:
        flash("Invalid verification code. Please try again.", "error")
        return redirect(url_for("delete_account"))


@celery.task(bind=True, ignore_result=True, max_retries=3, retry_backoff=True)
def send_contact_emails(self, name, email, message, subject):
    with app.app_context():
        try:
            logger.info(f"Starting contact email task for {email} with task_id {self.request.id}")
            
            # Validate email format
            if not re.match(EMAIL_REGEX, email):
                logger.error(f"Invalid email format: {email}")
                return
            
            # Sanitize user inputs
            sanitized_name = bleach.clean(name.title(), tags=[], strip=True)
            sanitized_email = bleach.clean(email, tags=[], strip=True)
            sanitized_message = bleach.clean(message, tags=['br'], strip=True)
            sanitized_subject = bleach.clean(subject, tags=[], strip=True)
            
            # Support email to support@inspirahub.com
            support_email = "support@inspirahub.com"
            support_subject = f"Support Request: {sanitized_subject} from {sanitized_name}"
            support_body = (
                f"Name: {sanitized_name}\n"
                f"Email: {sanitized_email}\n"
                f"Subject: {sanitized_subject}\n"
                f"Message:\n{sanitized_message}\n\n"
                f"Please respond to the user at {sanitized_email}."
            )
            support_msg = Message(
                support_subject,
                sender=app.config.get("MAIL_DEFAULT_SENDER", support_email),
                recipients=[support_email],
                reply_to=sanitized_email
            )
            support_msg.body = support_body
            
            logger.debug(f"Sending support email to {support_email}")
            mail.send(support_msg)
            logger.info(f"Support email sent to {support_email} from {sanitized_email}")
            
            # Auto-reply to user
            reply_subject = "Inspirahub: Thank You for Contacting Us!"
            plain_text_body = (
                f"Dear {sanitized_name},\n\n"
                f"Thank you for contacting the Inspirahub Support Team! We've received your message:\n\n"
                f"Subject: {sanitized_subject}\n"
                f"Message: {sanitized_message}\n\n"
                f"We'll respond within 24-48 hours. For common questions, visit our FAQ: https://www.inspirahub.com/faq\n"
                f"For urgent issues, reply to this email.\n\n"
                f"Best regards,\n"
                f"The Inspirahub Team\n"
                f"Inspirahub - Connecting Communities\n"
                f"https://www.inspirahub.com"
            )
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Inspirahub Support</title>
            </head>
            <body style="font-family: 'Helvetica Neue', Arial, sans-serif; color: #333333; background-color: #f9f9f9; padding: 20px; margin: 0;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden;">
                    <div style="background: linear-gradient(90deg, #3182ce, #2b6cb0); color: #ffffff; padding: 25px; text-align: center;">
                        <h1 style="margin: 0; font-size: 28px; font-weight: 600;">Inspirahub</h1>
                        <p style="margin: 8px 0 0; font-size: 16px; opacity: 0.9;">Support Team</p>
                    </div>
                    <div style="padding: 30px;">
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">Dear {sanitized_name},</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Thank you for contacting the Inspirahub Support Team! We've received your message:
                        </p>
                        <table style="width: 100%; border: 1px solid #e2e8f0; border-collapse: collapse; margin: 20px 0;">
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 10px; font-weight: 500; width: 25%; border: 1px solid #e2e8f0;">Subject</td>
                                <td style="padding: 10px; border: 1px solid #e2e8f0;">{sanitized_subject}</td>
                            </tr>
                            <tr>
                                <td style="padding: 10px; font-weight: 500; width: 25%; border: 1px solid #e2e8f0;">Message</td>
                                <td style="padding: 10px; border: 1px solid #e2e8f0;">{sanitized_message}</td>
                            </tr>
                        </table>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            Our team will respond within <strong>24-48 hours</strong>. For immediate answers, check out our 
                            <a href="https://www.inspirahub.com/faq" style="color: #2b6cb0; text-decoration: none; font-weight: 500;">FAQ page</a>.
                        </p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0 0 16px;">
                            For urgent issues, simply reply to this email, and we'll prioritize your request.
                        </p>
                        <div style="text-align: center; margin: 20px 0;">
                            <a href="https://www.inspirahub.com/faq" style="display: inline-block; padding: 12px 24px; background-color: #2b6cb0; color: #ffffff; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: 500;">
                                Visit Our FAQ
                            </a>
                        </div>
                        <p style="font-size: 16px; line-height: 1.6; margin: 0;">Best regards,</p>
                        <p style="font-size: 16px; line-height: 1.6; margin: 5px 0 0;">The Inspirahub Team</p>
                    </div>
                    <div style="background-color: #bee3f8; padding: 15px; text-align: center; font-size: 12px; color: #2a4365;">
                        <p style="margin: 0;">Inspirahub - Connecting Communities</p>
                        <p style="margin: 5px 0 0;">
                            <a href="https://www.inspirahub.com" style="color: #2b6cb0; text-decoration: none;">www.inspirahub.com</a> | 
                            <a href="mailto:{support_email}" style="color: #2b6cb0; text-decoration: none;">Contact Support</a>
                        </p>
                        <p style="margin: 5px 0 0; opacity: 0.7;">
                            This message was sent to {sanitized_email} in response to your support request.
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Create and send auto-reply email
            reply_msg = Message(
                reply_subject,
                sender=app.config.get("MAIL_DEFAULT_SENDER", support_email),
                recipients=[sanitized_email],
                reply_to=support_email
            )
            reply_msg.body = plain_text_body
            reply_msg.html = html_body
            
            logger.debug(f"Sending auto-reply email to {sanitized_email}")
            mail.send(reply_msg)
            logger.info(f"Auto-reply email sent to {sanitized_email}")
        
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error in send_contact_emails for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP recipients refused in send_contact_emails for {sanitized_email}: {str(e)}", exc_info=True)
            return
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error in send_contact_emails for {sanitized_email}: {type(e).__name__}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)
        except Exception as e:
            logger.error(f"Unexpected error in send_contact_emails for {sanitized_email}: {str(e)}", exc_info=True)
            self.retry(countdown=60, exc=e)


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        try:
            name = request.form.get("name")
            email = request.form.get("email")
            message = request.form.get("message")
            subject = request.form.get("subject")

            # Validate form inputs
            if not all([name, email, message, subject]):
                flash("All fields are required.", "error")
                return redirect(url_for("contact"))
            
            if not re.match(EMAIL_REGEX, email):
                flash("Invalid email format.", "error")
                return redirect(url_for("contact"))

            # Log form submission
            logger.info(f"Received contact form submission from {email}, subject: {subject}")

            # Queue the Celery task
            task = send_contact_emails.delay(name, email, message, subject)
            logger.info(f"Queued contact form email task with task_id {task.id} for {email}")

            flash(
                "Your message has been sent! Check your inbox for a confirmation.",
                "success"
            )
            return redirect(url_for("index"))

        except Exception as e:
            logger.error(f"Error queuing contact form email task: {str(e)}", exc_info=True)
            flash("Failed to send message. Please try again later.", "error")
            return redirect(url_for("contact"))

    return render_template("insipirahub/contact.html")


def get_user_posts(user_id, page=1, posts_per_page=2):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                offset = (page - 1) * posts_per_page
                query = """
                    SELECT posts.id, posts.title, posts.content, posts.created_at, posts.edited_at, 
                    accounts.username, accounts.profile_picture, COUNT(likes.id) as num_likes
                    FROM posts
                    JOIN accounts ON posts.user_id = accounts.id
                    LEFT JOIN likes ON posts.id = likes.post_id
                    WHERE posts.user_id = %s
                    GROUP BY posts.id, accounts.username, accounts.profile_picture
                    ORDER BY posts.created_at DESC
                    LIMIT %s OFFSET %s
                """
                cursor.execute(query, (user_id, posts_per_page, offset))
                posts = []
                for (
                    post_id,
                    title,
                    content,
                    created_at,
                    edited_at,
                    username,
                    profile_picture,
                    num_likes,
                ) in cursor.fetchall():
                    post = {
                        "id": post_id,
                        "title": title,
                        "content": content,
                        "created_at": created_at,
                        "edited_at": edited_at,
                        "username": username,
                        "profile_picture": profile_picture,
                        "num_likes": num_likes,
                    }
                    posts.append(post)
                return posts
    except psycopg2.Error as e:
        logger.error(f"Database error in get_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error in get_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return []


def get_total_user_posts(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = "SELECT COUNT(*) FROM posts WHERE user_id = %s"
                cursor.execute(query, (user_id,))
                total_posts = cursor.fetchone()[0]
                return total_posts
    except psycopg2.Error as e:
        logger.error(f"Database error in get_total_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return 0
    except Exception as e:
        logger.error(f"Unexpected error in get_total_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return 0


def get_your_posts(
    user_id, page, posts_per_page, search_title=None, search_category=None
):
    title_words = []  # Define locally, remove global title_words
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT p.id, p.title, p.content, a.username AS post_owner, p.created_at, p.edited_at, a.profile_picture
                    FROM posts p
                    JOIN accounts a ON p.user_id = a.id
                    WHERE p.user_id = %s
                """
                if search_title:
                    title_words = search_title.split()
                    title_conditions = " AND ".join(
                        [f"p.title ILIKE %s" for _ in title_words])
                    query += f" AND ({title_conditions})"
                if search_category:
                    query += " AND p.category ILIKE %s"

                query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"

                offset = (page - 1) * posts_per_page
                if offset < 0:
                    offset = 0

                params = [user_id]
                if search_title:
                    params.extend([f"%{word}%" for word in title_words])
                if search_category:
                    params.append(f"%{search_category}%")
                params.extend([posts_per_page, offset])

                cursor.execute(query, params)

                columns = [
                    "id",
                    "title",
                    "content",
                    "username",
                    "created_at",
                    "edited_at",
                    "profile_picture",
                ]
                posts_data = [dict(zip(columns, row)) for row in cursor.fetchall()]

                for post in posts_data:
                    post["content"] = escape(post["content"])

                return posts_data
    except psycopg2.Error as e:
        logger.error(f"Database error in get_your_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error in get_your_posts for user_id {user_id}: {str(e)}", exc_info=True)
        return []


@app.route("/your_posts", methods=["GET"])
def your_posts():
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    page = request.args.get("page", 1, type=int)
    posts_per_page = 2
    search_title = request.args.get("search_title")
    search_category = request.args.get("search_category")

    user_posts = get_your_posts(
        user_id, page, posts_per_page, search_title, search_category
    )
    total_posts = get_total_user_posts(user_id)

    return render_template(
        "posts/your_posts.html",
        user_posts=user_posts,
        total_posts=total_posts,
        posts_per_page=posts_per_page,
        page=page,
        max=max,
        min=min,
    )


@app.route("/create_post", methods=["GET", "POST"])
def create_post():
    logger.info(f"Accessing /create_post with method: {request.method}")
    if request.method == "POST":
        logger.info("Processing POST request for /create_post")
        if "user_id" not in session:
            logger.error("User not logged in, redirecting to login")
            flash("You need to log in first.", "error")
            return redirect(url_for("login"))

        user_id = session["user_id"]
        email = session.get("email", "")
        first_name = session.get("first_name", "")
        last_name = session.get("last_name", "")
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        display_style = request.form.get("display_style", "")
        category = request.form.get("category", "")

        logger.info(f"Form data: user_id={user_id}, title={title[:50]}, content_length={len(content)}, display_style={display_style}, category={category}")

        if not all([title, content, display_style, category]):
            logger.error(f"Missing form fields: title={'<empty>' if not title else '<present>'}, content={'<empty>' if not content else '<present>'}, display_style={'<empty>' if not display_style else '<present>'}, category={'<empty>' if not category else '<present>'}")
            flash("All fields are required.", "error")
            return redirect(url_for("create_post"))

        if len(title) > 50:
            logger.error("Title exceeds 50 characters")
            flash("Title must be 50 characters or less.", "error")
            return redirect(url_for("create_post"))
        if len(content) > 50500:
            logger.error("Content exceeds 50500 characters")
            flash("Content must be 50,500 characters or less.", "error")
            return redirect(url_for("create_post"))

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO posts (user_id, email, first_name, last_name, title, content, display_style, category, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        RETURNING id
                        """,
                        (user_id, email, first_name, last_name, title, content, display_style, category)
                    )
                    post_id = cursor.fetchone()[0]
                    conn.commit()
                    logger.info(f"Post created successfully: post_id={post_id}, user_id={user_id}, title={title}")
            flash("Post created successfully!", "success")
            return redirect(url_for("view_posts"))
        except psycopg2.Error as e:
            logger.error(f"Database error creating post: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("A database error occurred while creating the post. Please try again.", "error")
            return redirect(url_for("create_post"))
        except Exception as e:
            logger.error(f"Unexpected error creating post: {str(e)}", exc_info=True)
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("create_post"))
    else:
        logger.info("Rendering create_post_form.html for GET request")
        return render_template("posts/create_post_form.html")



# Custom Jinja2 filter for ordinal date
def ordinal_date(date):
    if date is None:
        return ""
    def get_ordinal_suffix(day):
        if 10 <= day % 100 <= 20:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        return suffix
    day = date.day
    suffix = get_ordinal_suffix(day)
    return date.strftime(f'%d{suffix} %B %Y')

# Register the filter with Jinja2
app.jinja_env.filters['ordinal_date'] = ordinal_date



@app.route("/view_posts", methods=["GET"])
def view_posts():
    page = request.args.get("page", 1, type=int)
    posts_per_page = 2
    search_query = request.args.get("q", "").strip()
    category = request.args.get("category", "all")

    offset = (page - 1) * posts_per_page
    sql_condition = ""
    placeholders = []

    if category == "title":
        sql_condition = "LOWER(posts.title) LIKE LOWER(%s)"
        placeholders = [f"%{search_query}%"]
    elif category == "content":
        sql_condition = "LOWER(posts.content) LIKE LOWER(%s)"
        placeholders = [f"%{search_query}%"]
    elif category == "author":
        sql_condition = "LOWER(accounts.username) LIKE LOWER(%s)"
        placeholders = [f"%{search_query}%"]
    elif category == "all":
        sql_condition = (
            "LOWER(posts.title) LIKE LOWER(%s) OR LOWER(posts.content) LIKE LOWER(%s)"
            " OR LOWER(accounts.username) LIKE LOWER(%s)"
        )
        placeholders = [f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"]

    sql_query = f"""
        SELECT posts.id, posts.content, posts.created_at, posts.edited_at, posts.title, 
               accounts.username, accounts.profile_picture, COUNT(likes.id) as num_likes, 
               (posts.edited_at IS NOT NULL) as is_edited, posts.user_id 
        FROM posts 
        LEFT JOIN accounts ON posts.user_id = accounts.id 
        LEFT JOIN likes ON posts.id = likes.post_id 
        WHERE {sql_condition}
        GROUP BY posts.id, accounts.username, accounts.profile_picture 
        ORDER BY COALESCE(posts.edited_at, posts.created_at) DESC 
        LIMIT %s OFFSET %s
    """
    placeholders.extend([posts_per_page, offset])

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_query, tuple(placeholders))
                posts_data = cursor.fetchall()

                count_query = f"""
                    SELECT COUNT(DISTINCT posts.id)
                    FROM posts
                    LEFT JOIN accounts ON posts.user_id = accounts.id
                    LEFT JOIN likes ON posts.id = likes.post_id
                    WHERE {sql_condition}
                """
                cursor.execute(count_query, tuple(placeholders[:-2]))
                total_posts = cursor.fetchone()[0]

                total_pages = ceil(total_posts / posts_per_page)

                if total_posts == 0:
                    no_results_message = f"No results found for '{search_query}'."
                    return render_template("posts/view_posts.html", no_results_message=no_results_message)

                search_results_message = f"Your search has resulted in {total_posts} result(s)."

                Post = namedtuple(
                    "Post",
                    [
                        "id",
                        "content",
                        "created_at",
                        "edited_at",
                        "title",
                        "username",
                        "profile_picture",
                        "num_likes",
                        "is_edited",
                        "user_id",
                    ],
                )
                posts = [
                    Post(
                        id=post[0],
                        content=post[1],
                        created_at=post[2],
                        edited_at=post[3],
                        title=post[4],
                        username=post[5],
                        profile_picture=post[6] if post[6] else "default_profile_image.png",
                        num_likes=post[7],
                        is_edited=post[8],
                        user_id=post[9],
                    )
                    for post in posts_data
                ]

                logger.info(f"Total Posts: {total_posts}")
                return render_template(
                    "posts/view_posts.html",
                    posts=posts,
                    current_page=page,
                    total_pages=total_pages,
                    pagination_range=range(1, total_pages + 1),
                    search_query=search_query,
                    selected_category=category,
                    search_results_message=search_results_message,
                    total_posts=total_posts,
                )
    except psycopg2.Error as e:
        logger.error(f"Database error in view_posts: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred while fetching posts. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in view_posts: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))



@app.route("/view_user_posts/<int:user_id>", methods=["GET"])
def view_user_posts(user_id):
    page = request.args.get("page", 1, type=int)
    posts_per_page = 2

    posts = get_user_posts(user_id, page, posts_per_page)
    total_posts = get_total_user_posts(user_id)
    total_pages = math.ceil(total_posts / posts_per_page)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT profile_picture FROM accounts WHERE id = %s", (user_id,))
                profile_picture = cursor.fetchone()[0]

                # Ensure profile_picture has a default value
                profile_picture = profile_picture if profile_picture else "default_profile_image.png"

                # Ensure each post has a profile picture (default if None)
                for post in posts:
                    if not post["profile_picture"]:
                        post["profile_picture"] = "default_profile_image.png"

                return render_template(
                    "posts/user_posts.html",
                    total_pages=total_pages,
                    posts=posts,
                    user_id=user_id,
                    profile_picture=profile_picture,
                    total_posts=total_posts,
                    page=page,
                    posts_per_page=posts_per_page,
                )
    except psycopg2.Error as e:
        logger.error(f"Database error in view_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching user profile. Please try again.", "error")
        return redirect(url_for("dashboard"))
    except Exception as e:
        logger.error(f"Unexpected error in view_user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("dashboard"))

@app.route("/like_post/<int:post_id>", methods=["POST"])
def like_post(post_id):
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "User not logged in"})

    user_id = session["user_id"]
    new_like_status = False

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check for existing like
                cursor.execute(
                    "SELECT id, like_status FROM likes WHERE post_id = %s AND user_id = %s",
                    (post_id, user_id),
                )
                existing_like = cursor.fetchone()

                # Get post title
                cursor.execute("SELECT title FROM posts WHERE id = %s", (post_id,))
                post_title = cursor.fetchone()
                if not post_title:
                    logger.warning(f"Post not found for post_id {post_id}")
                    return jsonify({"status": "error", "message": "Post not found"})

                post_title = post_title[0]

                # Get username
                cursor.execute(
                    "SELECT username FROM accounts WHERE id = %s", (user_id,))
                username = cursor.fetchone()
                if not username:
                    logger.warning(f"User not found for user_id {user_id}")
                    return jsonify({"status": "error", "message": "User not found"})

                username = username[0]

                if existing_like:
                    like_id, like_status = existing_like
                    new_like_status = not like_status
                    cursor.execute(
                        "UPDATE likes SET like_status = %s, post_title = %s, username = %s WHERE id = %s",
                        (new_like_status, post_title, username, like_id),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO likes (post_id, user_id, like_status, post_title, username) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (post_id, user_id, True, post_title, username),
                    )

                conn.commit()

                # Get updated like count
                cursor.execute(
                    "SELECT COUNT(id) FROM likes WHERE post_id = %s AND like_status = TRUE",
                    (post_id,),
                )
                num_likes = cursor.fetchone()[0]

                logger.info(f"Post {post_id} {'liked' if new_like_status else 'disliked'} by user_id {user_id}")
                return jsonify({
                    "status": "liked" if new_like_status else "disliked",
                    "num_likes": num_likes
                })
    except psycopg2.Error as e:
        logger.error(f"Database error in like_post for post_id {post_id}, user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({"status": "error", "message": "Database error occurred"})
    except Exception as e:
        logger.error(f"Unexpected error in like_post for post_id {post_id}, user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({"status": "error", "message": "An unexpected error occurred"})

        
@app.route("/delete_post/<int:post_id>", methods=["POST"])
def delete_post(post_id):
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
                post_owner_id = cursor.fetchone()

                if post_owner_id and post_owner_id[0] == user_id:
                    cursor.execute("DELETE FROM posts WHERE id = %s", (post_id,))
                    conn.commit()
                    logger.info(f"Post {post_id} deleted by user_id {user_id}")
                    flash("Post deleted successfully!", "success")
                else:
                    logger.warning(f"User {user_id} attempted to delete post {post_id} without permission")
                    flash("You do not have permission to delete this post.", "error")

                return redirect(url_for("view_posts"))
    except psycopg2.Error as e:
        logger.error(f"Database error in delete_post for post_id {post_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred while deleting the post. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in delete_post for post_id {post_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))


@app.route("/edit_post/<int:post_id>", methods=["GET", "POST"])
def edit_post(post_id):
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    print(f"[DEBUG] Entering edit_post for post_id={post_id} by user_id={user_id}")

    if request.method == "POST":
        new_content = request.form["content"]
        new_title = request.form["title"]
        print(f"[DEBUG] POST request to edit post_id={post_id} with title='{new_title}' and content='{new_content}'")

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # Vulnerable: No ownership check. BAC
                    print(f"[DEBUG] Attempting to update post_id={post_id} without ownership check")
                    edited_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(
                        "UPDATE posts SET content = %s, title = %s, edited_at = %s, is_edited = TRUE WHERE id = %s",
                        (new_content, new_title, edited_at, post_id),
                    )
                    conn.commit()
                    logger.info(f"Post {post_id} updated by user_id {user_id}")
                    print(f"[DEBUG] Successfully updated post_id={post_id} by user_id={user_id}")
                    flash("Post updated successfully!", "success")
                    return redirect(url_for("view_posts"))
        except psycopg2.Error as e:
            logger.error(f"Database error in edit_post for post_id {post_id}: {str(e)}", exc_info=True)
            print(f"[DEBUG] Database error in edit_post for post_id={post_id}: {str(e)}")
            if 'conn' in locals():
                conn.rollback()
            flash("A database error occurred while updating the post. Please try again.", "error")
            return redirect(url_for("view_posts"))
        except Exception as e:
            logger.error(f"Unexpected error in edit_post for post_id {post_id}: {str(e)}", exc_info=True)
            print(f"[DEBUG] Unexpected error in edit_post for post_id={post_id}: {str(e)}")
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("view_posts"))
    else:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    print(f"[DEBUG] GET request to fetch post_id={post_id} for editing")
                    cursor.execute(
                        "SELECT content, title FROM posts WHERE id = %s", (post_id,))
                    post_data = cursor.fetchone()

                    if post_data:
                        content = post_data[0]
                        title = post_data[1]
                        print(f"[DEBUG] Fetched post_id={post_id} with title='{title}'")
                        return render_template(
                            "posts/edit_post_form.html", post_id=post_id, content=content, title=title
                        )
                    else:
                        logger.warning(f"Post {post_id} not found for user_id {user_id}")
                        print(f"[DEBUG] Post {post_id} not found for user_id={user_id}")
                        flash("Post not found.", "error")
                        return redirect(url_for("view_posts"))
        except psycopg2.Error as e:
            logger.error(f"Database error in edit_post (GET) for post_id {post_id}: {str(e)}", exc_info=True)
            print(f"[DEBUG] Database error in edit_post (GET) for post_id={post_id}: {str(e)}")
            flash("A database error occurred while fetching the post. Please try again.", "error")
            return redirect(url_for("view_posts"))
        except Exception as e:
            logger.error(f"Unexpected error in edit_post (GET) for post_id {post_id}: {str(e)}", exc_info=True)
            print(f"[DEBUG] Unexpected error in edit_post (GET) for post_id={post_id}: {str(e)}")
            if 'conn' in locals():
                conn.rollback()
            flash("An unexpected error occurred. Please try again.", "error")
            return redirect(url_for("view_posts"))


@app.route("/user_posts", methods=["GET"])
def user_posts():
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    page = request.args.get("page", 1, type=int)
    posts_per_page = 2
    offset = (page - 1) * posts_per_page

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM posts WHERE user_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (user_id, posts_per_page, offset),
                )
                paginated_posts = cursor.fetchall()

                total_posts = get_total_user_posts(user_id)

                return render_template(
                    "posts/user_posts.html",
                    posts=paginated_posts,
                    page=page,
                    total_posts=total_posts,
                    posts_per_page=posts_per_page,
                )
    except psycopg2.Error as e:
        logger.error(f"Database error in user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching posts. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in user_posts for user_id {user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))


@app.route("/follow/<int:user_id>", methods=["GET", "POST"])
def follow_user(user_id):
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("following"))

    follower_id = session["user_id"]

    if follower_id == user_id:
        flash("You cannot follow yourself.", "error")
        return redirect(url_for("following"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM followers WHERE follower_id = %s AND following_id = %s",
                    (follower_id, user_id),
                )
                existing_follow = cursor.fetchone()

                if not existing_follow:
                    cursor.execute(
                        "INSERT INTO followers (follower_id, following_id) VALUES (%s, %s)",
                        (follower_id, user_id),
                    )
                    conn.commit()
                    logger.info(f"User {follower_id} followed user {user_id}")
                    flash(f"You are now following user {user_id}", "success")
                else:
                    logger.info(f"User {follower_id} already follows user {user_id}")
                    flash(f"You are already following user {user_id}", "info")

                return redirect(url_for("following"))
    except psycopg2.Error as e:
        logger.error(f"Database error in follow_user for user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred while following the user. Please try again.", "error")
        return redirect(url_for("following"))
    except Exception as e:
        logger.error(f"Unexpected error in follow_user for user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("following"))


@app.route("/unfollow/<int:user_id>", methods=["GET", "POST"])
def unfollow_user(user_id):
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        return redirect(url_for("following"))

    follower_id = session["user_id"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM followers WHERE follower_id = %s AND following_id = %s",
                    (follower_id, user_id),
                )
                existing_relationship = cursor.fetchone()

                if existing_relationship:
                    cursor.execute(
                        "DELETE FROM followers WHERE follower_id = %s AND following_id = %s",
                        (follower_id, user_id),
                    )
                    conn.commit()
                    logger.info(f"User {follower_id} unfollowed user {user_id}")
                    flash(f"You have unfollowed user {user_id}", "success")
                else:
                    logger.info(f"User {follower_id} is not following user {user_id}")
                    flash(f"You are not following user {user_id}", "error")

                return redirect(url_for("following"))
    except psycopg2.Error as e:
        logger.error(f"Database error in unfollow_user for user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred while unfollowing the user. Please try again.", "error")
        return redirect(url_for("following"))
    except Exception as e:
        logger.error(f"Unexpected error in unfollow_user for user_id {user_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("following"))


@app.route("/following")
def following():
    if "user_id" not in session:
        flash("You need to login first.", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT a.username, a.id FROM accounts a "
                    "JOIN followers f ON a.id = f.following_id "
                    "WHERE f.follower_id = %s",
                    (user_id,),
                )
                following_users_data = cursor.fetchall()

                following_users = [
                    {"username": user_data[0], "id": user_data[1]}
                    for user_data in following_users_data
                ]

                logger.info(f"User ID: {user_id}")
                logger.info(f"Following Users' Data: {following_users}")

                return render_template("social/following.html", following_users=following_users)
    except psycopg2.Error as e:
        logger.error(f"Database error in following for user_id {user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching following users. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in following for user_id {user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))

        
@app.route("/followers_profile/<int:user_id>")
def followers_profile(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT username FROM accounts WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()

                if user_data:
                    username = user_data[0]

                    cursor.execute(
                        "SELECT a.id, a.username FROM accounts a "
                        "JOIN followers f ON a.id = f.follower_id "
                        "WHERE f.following_id = %s",
                        (user_id,),
                    )
                    followers_data = cursor.fetchall()

                    follower_usernames = []
                    logged_in_user_id = session.get("user_id")
                    for follower in followers_data:
                        follower_id, follower_username = follower
                        # Check if the logged-in user follows this follower
                        cursor.execute(
                            "SELECT COUNT(*) FROM followers WHERE follower_id = %s AND following_id = %s",
                            (logged_in_user_id, follower_id),
                        )
                        is_following = cursor.fetchone()[0] > 0
                        follower_usernames.append({
                            "id": follower_id,
                            "username": follower_username,
                            "is_following": is_following
                        })

                    return render_template(
                        "social/followers_profile.html",
                        username=username,
                        follower_usernames=follower_usernames,
                    )
                else:
                    logger.warning(f"User not found for user_id {user_id}")
                    flash("User not found.", "error")
                    return redirect(url_for("login"))
    except psycopg2.Error as e:
        logger.error(f"Database error in followers_profile for user_id {user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching followers. Please try again.", "error")
        return redirect(url_for("dashboard"))
    except Exception as e:
        logger.error(f"Unexpected error in followers_profile for user_id {user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("dashboard"))


@app.route("/followers")
def followers():
    if "user_id" not in session:
        flash("You need to login first.", "error")
        return redirect(url_for("login"))

    logged_in_user_id = session["user_id"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT a.username, a.id FROM accounts a "
                    "JOIN followers f ON a.id = f.follower_id "
                    "WHERE f.following_id = %s",
                    (logged_in_user_id,),
                )
                followers_data = cursor.fetchall()

                logger.info(f"Fetched followers' data for user_id {logged_in_user_id}: {followers_data}")

                return render_template("social/following.html", followers=followers_data)
    except psycopg2.Error as e:
        logger.error(f"Database error in followers for user_id {logged_in_user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching followers. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in followers for user_id {logged_in_user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))


@app.route("/public_profile/<int:user_id>")
def public_profile(user_id):
    page = request.args.get("page", 1, type=int)
    posts_per_page = 2

    is_following = check_if_user_is_following(session.get("user_id"), user_id)
    is_followed = check_if_user_is_following(user_id, session.get("user_id"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT username, profile_picture, registration_date FROM accounts WHERE id = %s",
                    (user_id,),
                )
                user_data = cursor.fetchone()

                if user_data:
                    username = user_data[0]
                    profile_picture_filename = user_data[1] or "default_profile_image.png"
                    profile_picture_url = url_for(
                        "uploaded_file", filename=profile_picture_filename
                    )
                    registration_date = user_data[2]

                    formatted_registration_date = registration_date.strftime("%B %Y, %d, %A")

                    followers_count = get_followers_count(user_id, cursor)
                    following_count = get_following_count(user_id, cursor)

                    # Fetch user's posts
                    posts = get_user_posts(user_id, page, posts_per_page)
                    total_posts = get_total_user_posts(user_id)
                    total_pages = math.ceil(total_posts / posts_per_page)
                    pagination_range = range(1, total_pages + 1)

                    # Convert posts to named tuple for consistency with view_posts
                    Post = namedtuple(
                        "Post",
                        [
                            "id",
                            "title",
                            "content",
                            "created_at",
                            "edited_at",
                            "username",
                            "profile_picture",
                            "num_likes",
                            "is_edited",
                            "user_id",
                        ],
                    )
                    formatted_posts = [
                        Post(
                            id=post["id"],
                            title=post["title"],
                            content=post["content"],
                            created_at=post["created_at"],
                            edited_at=post["edited_at"],
                            username=username,
                            profile_picture=profile_picture_filename,
                            num_likes=post["num_likes"],
                            is_edited=(post["edited_at"] is not None),
                            user_id=user_id,
                        )
                        for post in posts
                    ]

                    logger.info(
                        f"User {username} has {followers_count} followers and {following_count} following."
                    )

                    return render_template(
                        "account/public_profile.html",
                        username=username,
                        profile_picture=profile_picture_url,
                        registration_date=formatted_registration_date,
                        is_following=is_following,
                        is_followed=is_followed,
                        followers_count=followers_count,
                        following_count=following_count,
                        user_id=user_id,
                        posts=formatted_posts,
                        current_page=page,
                        pagination_range=pagination_range,
                    )
                else:
                    logger.warning(f"User not found for user_id {user_id}")
                    flash("User not found.", "error")
                    return redirect(url_for("login"))
    except psycopg2.Error as e:
        logger.error(f"Database error in public_profile for user_id {user_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching the profile. Please try again.", "error")
        return redirect(url_for("login"))
    except Exception as e:
        logger.error(f"Unexpected error in public_profile for user_id {user_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("login"))



def check_if_user_is_following(follower_id, following_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM followers WHERE follower_id = %s AND following_id = %s",
                    (follower_id, following_id),
                )
                count = cursor.fetchone()[0]
                return count > 0
    except psycopg2.Error as e:
        logger.error(f"Database error in check_if_user_is_following: {str(e)}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error in check_if_user_is_following: {str(e)}", exc_info=True)
        return False



@app.route("/full_post/<int:post_id>", methods=["GET"])
def full_post(post_id):
    user_id = session.get("user_id")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
                post_owner_id = cursor.fetchone()
                if not post_owner_id:
                    logger.warning(f"Post {post_id} not found")
                    flash("Post not found.", "error")
                    return redirect(url_for("view_posts"))

                is_following = False
                if user_id:
                    cursor.execute(
                        "SELECT * FROM followers WHERE follower_id = %s AND following_id = %s",
                        (user_id, post_owner_id[0])
                    )
                    is_following = cursor.fetchone() is not None

                cursor.execute(
                    """
                    SELECT p.title, p.content, a.username, p.created_at, p.edited_at, p.is_edited, a.profile_picture
                    FROM posts p
                    JOIN accounts a ON p.user_id = a.id
                    WHERE p.id = %s
                    """,
                    (post_id,)
                )
                post_data = cursor.fetchone()
                if not post_data:
                    logger.warning(f"Post {post_id} not found")
                    flash("Post not found.", "error")
                    return redirect(url_for("view_posts"))

                title = post_data[0]
                content = post_data[1]
                username = post_data[2]
                created_at = post_data[3]
                edited_at = post_data[4]
                is_edited = post_data[5]
                profile_picture = post_data[6] if post_data[6] else "default_profile_image.png"

                cursor.execute(
                    "SELECT c.content, a.username FROM comments c JOIN accounts a ON c.user_id = a.id WHERE c.post_id = %s",
                    (post_id,)
                )
                comments_data = cursor.fetchall()

                cursor.execute(
                    "SELECT COUNT(*) FROM followers WHERE following_id = %s",
                    (post_owner_id[0],)
                )
                total_followers = cursor.fetchone()[0]

                Comment = namedtuple("Comment", ["content", "username"])
                comments = [Comment(content=comment[0], username=comment[1]) for comment in comments_data]

                return render_template(
                    "posts/full_post.html",
                    title=title,
                    content=content,
                    username=username,
                    created_at=created_at,
                    edited_at=edited_at,
                    is_edited=is_edited,
                    profile_picture=profile_picture,
                    post_id=post_id,
                    user_id=user_id,
                    post_owner_id=post_owner_id[0],
                    is_following=is_following,
                    total_followers=total_followers,
                    comments=comments
                )
    except psycopg2.Error as e:
        logger.error(f"Database error in full_post for post_id {post_id}: {str(e)}", exc_info=True)
        flash("A database error occurred while fetching the post. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in full_post for post_id {post_id}: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))
        
        
@app.route("/add_comment/<int:post_id>", methods=["POST"])
def add_comment(post_id):
    if "user_id" not in session:
        flash("You need to log in first.", "error")
        logger.info("User not logged in, redirecting to login")
        return redirect(url_for("full_post", post_id=post_id))

    user_id = session["user_id"]
    commenter_email = request.form["commenter_email"]
    content = request.form["comment_content"]

    logger.info(f"User ID: {user_id}")
    logger.info(f"Commenter Email: {commenter_email}")
    logger.info(f"Comment Content: {content}")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT username, email FROM accounts WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()

                cursor.execute("SELECT title FROM posts WHERE id = %s", (post_id,))
                post_data = cursor.fetchone()

                if user_data and user_data[1] == commenter_email and post_data:
                    commenter_username = user_data[0]
                    post_title = post_data[0]
                    logger.info(f"Post Title: {post_title}")

                    if content and post_title:
                        cursor.execute(
                            "INSERT INTO comments (post_id, user_id, username, email, content, post_title) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (
                                post_id,
                                user_id,
                                commenter_username,
                                commenter_email,
                                content,
                                post_title,
                            ),
                        )
                        conn.commit()
                        logger.info(f"Comment added to post {post_id} by user_id {user_id}")
                        flash("Comment added successfully!", "success")
                        return redirect(url_for("full_post", post_id=post_id))
                    else:
                        logger.warning("Comment content or post title is empty")
                        flash("Comment content and post title are required.", "error")
                        return redirect(url_for("full_post", post_id=post_id))
                else:
                    logger.warning("Invalid email address or post data")
                    flash(
                        "Invalid email address or post data. Please enter your own email address and "
                        "ensure the post exists.",
                        "error",
                    )
                    return redirect(url_for("full_post", post_id=post_id))
    except psycopg2.Error as e:
        logger.error(f"Database error in add_comment for post_id {post_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("A database error occurred while adding the comment. Please try again.", "error")
        return redirect(url_for("full_post", post_id=post_id))
    except Exception as e:
        logger.error(f"Unexpected error in add_comment for post_id {post_id}: {str(e)}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("full_post", post_id=post_id))


def retrieve_posts_by_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT p.id, p.title, p.content, a.profile_picture FROM posts p "
                    "JOIN accounts a ON p.user_id = a.id "
                    "WHERE p.user_id = %s",
                    (user_id,),
                )

                posts_data = cursor.fetchall()
                Post = namedtuple("Post", ["id", "title", "content", "profile_picture"])  # Define Post here
                posts = [
                    Post(id=row[0], title=row[1], content=row[2], profile_picture=row[3])
                    for row in posts_data
                ]
                for post in posts:
                    logger.info(
                        f"Post ID: {post.id}, Title: {post.title}, Content: {post.content},"
                        f" Profile Picture: {post.profile_picture}"
                    )

                return posts
    except psycopg2.Error as e:
        logger.error(f"Database error in retrieve_posts_by_user for user_id {user_id}: {str(e)}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error in retrieve_posts_by_user for user_id {user_id}: {str(e)}", exc_info=True)
        return []


@app.route("/follower_posts/<int:user_id>")
def follower_posts(user_id):
    page = request.args.get("page", default=1, type=int)
    per_page = 2

    posts = retrieve_posts_by_user(user_id)

    logger.info(f"User ID: {user_id}")
    logger.info(f"Number of Posts: {len(posts)}")
    for post in posts:
        logger.info(f"Post ID: {post.id}, Content: {post.content}")

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_posts = posts[start_idx:end_idx]

    return render_template(
        "posts/follower_posts.html",
        posts=paginated_posts,
        user_id=user_id,
        page=page,
        per_page=per_page,
    )


def retrieve_posts_by_following(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT p.id, p.title, p.content, a.profile_picture "
                    "FROM posts p "
                    "JOIN accounts a ON p.user_id = a.id "
                    "JOIN followers f ON p.user_id = f.follower_id "
                    "WHERE f.following_id = %s",
                    (user_id,),
                )

                posts_data = cursor.fetchall()
                Post = namedtuple("Post", ["id", "title", "content", "profile_picture"])  # Define Post here
                posts = [
                    Post(id=row[0], title=row[1], content=row[2], profile_picture=row[3])
                    for row in posts_data
                ]
                return posts
    except psycopg2.Error as e:
        logger.error(f"Database error in retrieve_posts_by_following for user_id {user_id}: {str(e)}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error in retrieve_posts_by_following for user_id {user_id}: {str(e)}", exc_info=True)
        return []


@app.route("/following_posts/<int:user_id>")
def following_posts(user_id):
    page = request.args.get("page", default=1, type=int)
    per_page = 2

    posts = retrieve_posts_by_user(user_id)

    logger.info(f"User ID: {user_id}")
    logger.info(f"Number of Posts: {len(posts)}")
    for post in posts:
        logger.info(f"Post ID: {post.id}, Content: {post.content}")

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_posts = posts[start_idx:end_idx]

    return render_template(
        "posts/following_posts.html",
        posts=paginated_posts,
        user_id=user_id,
        page=page,
        per_page=per_page,
    )


@app.route("/robots.txt")
def robots_txt():
    return send_file("static/robots.txt")


@app.route("/developers")
def developers():
    return render_template("developers.html")


@app.route("/admin", methods=["GET"])
@admin_required
def admin_panel():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT id, username, email, role, registration_date FROM accounts ORDER BY registration_date DESC")
        users = cursor.fetchall()
        user_list = [
            {
                "id": row[0],
                "username": row[1],
                "email": row[2],
                "role": row[3],
                "registration_date": row[4]
            } for row in users
        ]

        cursor.execute("""
            SELECT p.id, p.title, p.content, p.created_at, p.user_id, a.username
            FROM posts p JOIN accounts a ON p.user_id = a.id
            ORDER BY p.created_at DESC
        """)
        posts = cursor.fetchall()
        post_list = [
            {
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "created_at": row[3],
                "user_id": row[4],
                "username": row[5]
            } for row in posts
        ]

        cursor.close()
        logger.info(f"Admin {session.get('username')} accessed admin panel")
        return render_template("admin/panel.html", users=user_list, posts=post_list, current_user_role=session.get("role", "admin"))

    except psycopg2.Error as e:
        logger.error(f"Database error in admin_panel: {str(e)}", exc_info=True)
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))
    except Exception as e:
        logger.error(f"Unexpected error in admin_panel: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("view_posts"))


@app.route("/admin/delete_post/<int:post_id>", methods=["POST"])
@admin_required
def admin_delete_post(post_id):
    if "user_id" not in session:
        flash("You need to login first.", "error")
        return redirect(url_for("login"))
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
        post = cursor.fetchone()
        if not post:
            flash("Post not found.", "error")
            cursor.close()
            return redirect(url_for("admin_panel"))
        
        # Delete dependent likes and comments
        cursor.execute("DELETE FROM likes WHERE post_id = %s", (post_id,))
        cursor.execute("DELETE FROM comments WHERE post_id = %s", (post_id,))
        cursor.execute("DELETE FROM posts WHERE id = %s", (post_id,))
        conn.commit()

        logger.info(f"Post {post_id} deleted by user {session.get('username')}")
        flash("Post deleted successfully.", "success")

        cursor.close()
        return redirect(url_for("admin_panel"))

    except psycopg2.Error as e:
        logger.error(f"Database error in admin_delete_post: {str(e)}", exc_info=True)
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("admin_panel"))
    except Exception as e:
        logger.error(f"Unexpected error in admin_delete_post: {str(e)}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("admin_panel"))


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("You need to login first.", "error")
            return redirect(url_for("login"))
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT role FROM accounts WHERE id = %s", (session["user_id"],))
            result = cursor.fetchone()
            cursor.close()

            if result is None:
                flash("User not found.", "error")
                return redirect(url_for("login"))

            user_role = result[0]
            if user_role == "admin":
                return f(*args, **kwargs)
            else:
                logger.warning(f"Unauthorized admin access attempt by user_id: {session['user_id']}")
                flash("Access denied. You do not have permission to view this page.", "error")
                return redirect(url_for("view_posts"))

        except psycopg2.Error as e:
            logger.error(f"Database error in admin_required: {e}", exc_info=True)
            flash("A database error occurred. Please try again.", "error")
            return redirect(url_for("view_posts"))

    return decorated_function



@app.route("/admin/dashboard", methods=["GET"])
@admin_required
def admin_dashboard():
    logger.info(f"Admin {session.get('username')} accessed admin dashboard")
    return render_template("admin/admin_dashboard.html")


@app.route("/admin/create_user", methods=["GET", "POST"])
@admin_required
def admin_create_user():
    if request.method == "POST":
        email = request.form.get("email")
        first_name = request.form.get("first_name")
        last_name = request.form.get("last_name")
        username = request.form.get("username")
        password = request.form.get("password")
        country = request.form.get("country")
        role = request.form.get("role", "user")  # Default to user role

        if not all([email, first_name, last_name, username, password, country]):
            flash("All fields are required.", "error")
            return redirect(url_for("admin_create_user"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO accounts (email, first_name, last_name, username, password, country, role, user_verified, registration_date) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    email,
                    first_name,
                    last_name,
                    username,
                    generate_password_hash(password),
                    country,
                    role,
                    False,
                    datetime.now(),
                )
            )
            conn.commit()
            cursor.close()

            logger.info(f"Admin {session.get('username')} created user {username}")
            flash("User created successfully.", "success")
            return redirect(url_for("admin_dashboard"))

        except psycopg2.IntegrityError as e:
            logger.error(f"Integrity error in admin_create_user: {str(e)}", exc_info=True)
            flash("Username or email already exists.", "error")
            return redirect(url_for("admin_create_user"))
        except psycopg2.Error as e:
            logger.error(f"Database error in admin_create_user: {str(e)}", exc_info=True)
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("admin_create_user"))

    return render_template("admin/admin_create_user.html")


@app.route("/admin/custom_query", methods=["GET", "POST"])
@admin_required
def admin_custom_query():
    query = None
    result = None

    if request.method == "POST":
        sql_query = request.form.get("sql_query")

        if not sql_query:
            flash("Query is required.", "error")
            return redirect(url_for("admin_custom_query"))

        if not sql_query.strip().upper().startswith("SELECT"):
            flash("Only SELECT queries are allowed.", "error")
            return redirect(url_for("admin_custom_query"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(sql_query)

            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
                result = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            query = sql_query
            cursor.close()
            logger.info(f"Admin {session.get('username')} executed query: {sql_query}")

        except psycopg2.Error as e:
            logger.error(f"Database error in admin_custom_query: {str(e)}", exc_info=True)
            flash("Invalid query or database error.", "error")
            return redirect(url_for("admin_custom_query"))

    return render_template("admin/admin_custom_query.html", query=query, result=result)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        email = request.form.get("email")

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, password, role FROM accounts WHERE username = %s AND email = %s",
                (username, email)
            )
            user = cursor.fetchone()
            cursor.close()

            if user and check_password_hash(user[2], password):
                if user[3] != "admin":
                    flash("Access denied. Admin privileges required.", "error")
                    return redirect(url_for("admin_login"))

                session.clear()
                session["user_id"] = user[0]
                session["username"] = user[1]
                session["role"] = user[3]
                logger.info(f"Admin {username} logged in")
                return redirect(url_for("admin_dashboard"))

            flash("Invalid credentials.", "error")
            return redirect(url_for("admin_login"))

        except psycopg2.Error as e:
            logger.error(f"Database error in admin_login: {str(e)}", exc_info=True)
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("admin_login"))

    return render_template("admin/admin_login.html")


@app.route("/admin/logout", methods=["GET"])
@admin_required
def admin_logout():
    session.clear()
    logger.info(f"Admin {session.get('username')} logged out")
    flash("Logged out successfully.", "success")
    return redirect(url_for("admin_login"))

@app.route("/admin/view_users", methods=["GET"])
@admin_required
def admin_view_users():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, email, role, registration_date, first_name, last_name, country FROM accounts ORDER BY registration_date DESC"
        )
        users = cursor.fetchall()
        user_list = [
            {
                "id": row[0],
                "username": row[1],
                "email": row[2],
                "role": row[3],
                "registration_date": row[4],
                "first_name": row[5],
                "last_name": row[6],
                "country": row[7]
            } for row in users
        ]
        cursor.close()

        logger.info(f"Admin {session.get('username')} viewed user list")
        return render_template("admin/view_users.html", users=user_list)

    except psycopg2.Error as e:
        logger.error(f"Database error in admin_view_users: {str(e)}", exc_info=True)
        flash("Database error occurred. Please try again.", "error")
        return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_password", methods=["GET", "POST"])
@admin_required
def admin_reset_password():
    if request.method == "POST":
        user_id = request.form.get("user_id")
        new_password = request.form.get("new_password")

        if not all([user_id, new_password]):
            flash("User ID and new password are required.", "error")
            return redirect(url_for("admin_reset_password"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM accounts WHERE id = %s", (user_id,))
            if not cursor.fetchone():
                cursor.close()
                flash("User not found.", "error")
                return redirect(url_for("admin_reset_password"))

            cursor.execute(
                "UPDATE accounts SET password = %s WHERE id = %s",
                (generate_password_hash(new_password), user_id)
            )
            conn.commit()
            cursor.close()

            logger.info(f"Admin {session.get('username')} reset password for user_id {user_id}")
            flash("Password reset successfully.", "success")
            return redirect(url_for("admin_dashboard"))

        except psycopg2.Error as e:
            logger.error(f"Database error in admin_reset_password: {str(e)}", exc_info=True)
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("admin_reset_password"))

    return render_template("admin/reset_password.html")


@app.route("/admin/manage_roles", methods=["GET", "POST"])
@admin_required
def admin_manage_roles():
    if request.method == "POST":
        user_id = request.form.get("user_id")
        new_role = request.form.get("role")

        if not all([user_id, new_role]) or new_role not in ["user", "admin"]:
            flash("Valid user ID and role (user/admin) are required.", "error")
            return redirect(url_for("admin_manage_roles"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM accounts WHERE id = %s", (user_id,))
            if not cursor.fetchone():
                cursor.close()
                flash("User not found.", "error")
                return redirect(url_for("admin_manage_roles"))

            cursor.execute(
                "UPDATE accounts SET role = %s WHERE id = %s",
                (new_role, user_id)
            )
            conn.commit()
            cursor.close()

            logger.info(f"Admin {session.get('username')} changed role for user_id {user_id} to {new_role}")
            flash("User role updated successfully.", "success")
            return redirect(url_for("admin_dashboard"))

        except psycopg2.Error as e:
            logger.error(f"Database error in admin_manage_roles: {str(e)}", exc_info=True)
            flash("Database error occurred. Please try again.", "error")
            return redirect(url_for("admin_manage_roles"))

    return render_template("admin/manage_roles.html")



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
