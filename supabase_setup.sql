-- Bot Users Table
CREATE TABLE IF NOT EXISTS bot_users (
  user_id BIGINT PRIMARY KEY
);

-- Referral / Access Table
CREATE TABLE IF NOT EXISTS referral_db (
  user_id TEXT PRIMARY KEY,
  access_expires_at FLOAT DEFAULT 0.0,
  referrals JSONB DEFAULT '[]',
  referred_by TEXT DEFAULT NULL,
  referral_count INT DEFAULT 0,
  expired_notified BOOLEAN DEFAULT FALSE,
  cooldown_until FLOAT DEFAULT 0.0,
  captcha_verified BOOLEAN DEFAULT FALSE
);

-- Firebase List Table
CREATE TABLE IF NOT EXISTS global_firebases (
  id SERIAL PRIMARY KEY,
  url TEXT NOT NULL,
  tag TEXT NOT NULL
);

-- Bot Settings Table (maintenance, captcha, channels)
CREATE TABLE IF NOT EXISTS bot_settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL
);

-- Insert defaults
INSERT INTO bot_settings (key, value) VALUES
  ('maintenance_mode', 'false'),
  ('captcha_enabled', 'true'),
  ('required_channels', '[]')
ON CONFLICT (key) DO NOTHING;
