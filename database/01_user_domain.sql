-- ============================================
-- User Domain (用户域) - 5 tables
-- ============================================

-- 用户主表
CREATE TABLE users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(50),
    email VARCHAR(100),
    phone VARCHAR(20),
    registered_at TIMESTAMP NOT NULL,
    registration_source VARCHAR(50),  -- 'referral', 'organic', 'huawei_store', 'web', 'ad_campaign', 'wechat_mini', 'app_store', 'google_play'（小程序渠道叫 wechat_mini）
    status VARCHAR(20) DEFAULT 'active',  -- 'active', 'inactive', 'deleted', 'suspended'（封禁态叫 suspended）
    user_level INT DEFAULT 1,  -- 1-5 用户等级
    is_vip BOOLEAN DEFAULT FALSE,
    last_active_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户画像属性
CREATE TABLE user_profiles (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
    age INT,
    gender VARCHAR(10),  -- 'female', 'male'（业务上还有 unknown，本批数据没有）
    birth_date DATE,
    city VARCHAR(50),
    province VARCHAR(50),
    country VARCHAR(50) DEFAULT 'China',
    interests TEXT[],  -- array of interest tags
    occupation VARCHAR(50),
    income_level VARCHAR(20),  -- 'medium', 'low', 'high', 'very_high'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户设备信息
CREATE TABLE user_devices (
    device_id VARCHAR(100) PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    device_type VARCHAR(20),  -- 'ios', 'android', 'web', 'mini_program'
    os_version VARCHAR(20),
    device_model VARCHAR(50),
    device_brand VARCHAR(50),
    app_version VARCHAR(20),
    push_token VARCHAR(200),
    is_primary BOOLEAN DEFAULT FALSE,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户分群定义
CREATE TABLE user_segments (
    segment_id SERIAL PRIMARY KEY,
    segment_name VARCHAR(100) NOT NULL,
    -- 装的是分群维度，不是刷新方式（旧注释写的 static/dynamic 是后者）
    segment_type VARCHAR(50),  -- 'lifecycle', 'membership', 'behavior', 'demographic', 'value', 'device', 'geographic', 'engagement', 'acquisition'
    description TEXT,
    rules_json JSONB,  -- 分群规则定义
    owner VARCHAR(50),
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户分群成员关系
CREATE TABLE user_segment_members (
    user_id BIGINT REFERENCES users(user_id),
    segment_id INT REFERENCES user_segments(segment_id),
    entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exited_at TIMESTAMP,
    PRIMARY KEY (user_id, segment_id)
);

-- Indexes
CREATE INDEX idx_users_registered_at ON users(registered_at);
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_last_active ON users(last_active_at);
CREATE INDEX idx_user_devices_user_id ON user_devices(user_id);
CREATE INDEX idx_user_segment_members_segment ON user_segment_members(segment_id);
