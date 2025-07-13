-- Create the insipirahub database
CREATE DATABASE insipirahub;

-- Connect to the insipirahub database
\c insipirahub

-- Create accounts table
CREATE TABLE accounts (
    id INTEGER NOT NULL PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(100) NOT NULL,
    password VARCHAR(255) NOT NULL,
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    profile_picture VARCHAR(255),
    secret_key VARCHAR(255),
    country VARCHAR(100),
    day INTEGER,
    month INTEGER,
    year INTEGER,
    user_verified BOOLEAN DEFAULT FALSE,
    security_pin VARCHAR(8),
    tfa VARCHAR(1) DEFAULT 'F',
    registration_date TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    pin_deleted_at TIMESTAMP WITHOUT TIME ZONE,
    role VARCHAR(50) DEFAULT 'user',
    auth_token TEXT,
    ttmp TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT accounts_email_key UNIQUE (email),
    CONSTRAINT accounts_username_key UNIQUE (username),
    CONSTRAINT idx_security_pin UNIQUE (security_pin) WHERE pin_deleted_at IS NULL
);

-- Create sequence for accounts table
CREATE SEQUENCE accounts_id_seq OWNED BY accounts.id;
ALTER TABLE accounts ALTER COLUMN id SET DEFAULT nextval('accounts_id_seq');

-- Create comments table
CREATE TABLE comments (
    id INTEGER NOT NULL PRIMARY KEY,
    post_id INTEGER,
    user_id INTEGER,
    username VARCHAR(50),
    email VARCHAR(100),
    content TEXT NOT NULL,
    post_title VARCHAR(255),
    CONSTRAINT comments_post_id_fkey FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    CONSTRAINT comments_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id)
);

-- Create sequence for comments table
CREATE SEQUENCE comments_id_seq OWNED BY comments.id;
ALTER TABLE comments ALTER COLUMN id SET DEFAULT nextval('comments_id_seq');

-- Create deleted_accounts table
CREATE TABLE deleted_accounts (
    id INTEGER NOT NULL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    username VARCHAR(100),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    country VARCHAR(100),
    day INTEGER,
    month INTEGER,
    year INTEGER,
    deleted_date DATE NOT NULL,
    deletion_reason TEXT
);

-- Create sequence for deleted_accounts table
CREATE SEQUENCE deleted_accounts_id_seq OWNED BY deleted_accounts.id;
ALTER TABLE deleted_accounts ALTER COLUMN id SET DEFAULT nextval('deleted_accounts_id_seq');

-- Create followers table
CREATE TABLE followers (
    id INTEGER NOT NULL PRIMARY KEY,
    follower_id INTEGER,
    following_id INTEGER,
    CONSTRAINT followers_follower_id_fkey FOREIGN KEY (follower_id) REFERENCES accounts(id),
    CONSTRAINT followers_following_id_fkey FOREIGN KEY (following_id) REFERENCES accounts(id),
    CONSTRAINT followers_follower_id_following_id_key UNIQUE (follower_id, following_id)
);

-- Create sequence for followers table
CREATE SEQUENCE followers_id_seq OWNED BY followers.id;
ALTER TABLE followers ALTER COLUMN id SET DEFAULT nextval('followers_id_seq');

-- Create likes table
CREATE TABLE likes (
    id INTEGER NOT NULL PRIMARY KEY,
    post_id INTEGER,
    user_id INTEGER,
    like_status BOOLEAN NOT NULL,
    post_title VARCHAR(255),
    username VARCHAR(50),
    CONSTRAINT likes_post_id_fkey FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    CONSTRAINT likes_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id)
);

-- Create sequence for likes table
CREATE SEQUENCE likes_id_seq OWNED BY likes.id;
ALTER TABLE likes ALTER COLUMN id SET DEFAULT nextval('likes_id_seq');

-- Create posts table
CREATE TABLE posts (
    id INTEGER NOT NULL PRIMARY KEY,
    user_id INTEGER,
    email VARCHAR(100),
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    content TEXT NOT NULL,
    title VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    edited_at TIMESTAMP WITHOUT TIME ZONE,
    is_edited BOOLEAN DEFAULT FALSE,
    category VARCHAR(50),
    display_style TEXT,
    CONSTRAINT posts_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id)
);

-- Create sequence for posts table
CREATE SEQUENCE posts_id_seq OWNED BY posts.id;
ALTER TABLE posts ALTER COLUMN id SET DEFAULT nextval('posts_id_seq');

-- Create tokens table
CREATE TABLE tokens (
    id INTEGER NOT NULL PRIMARY KEY,
    account_id INTEGER,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL,
    verification_token VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    verification_sent_time TIMESTAMP WITHOUT TIME ZONE,
    verification_token_expiration TIMESTAMP WITHOUT TIME ZONE,
    reset_password_token VARCHAR(255),
    reset_password_token_expiration TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT tokens_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Create sequence for tokens table
CREATE SEQUENCE tokens_id_seq OWNED BY tokens.id;
ALTER TABLE tokens ALTER COLUMN id SET DEFAULT nextval('tokens_id_seq');

-- Create index for tokens table
CREATE INDEX idx_reset_password_token ON tokens (reset_password_token);

-- Create reset_tokens table
CREATE TABLE reset_tokens (
    id INTEGER NOT NULL PRIMARY KEY,
    account_id INTEGER,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL,
    reset_password_token VARCHAR(255) NOT NULL,
    reset_password_token_expiration TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    command_output TEXT,
    CONSTRAINT reset_tokens_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Create sequence for reset_tokens table
CREATE SEQUENCE reset_tokens_id_seq OWNED BY reset_tokens.id;
ALTER TABLE reset_tokens ALTER COLUMN id SET DEFAULT nextval('reset_tokens_id_seq');
