-- ============================================
-- Behavior Domain (行为域) - 4 tables
-- ============================================

-- 事件元数据/数据字典
CREATE TABLE event_definitions (
    event_name VARCHAR(100) PRIMARY KEY,
    event_category VARCHAR(50),  -- 'engagement', 'commerce', 'social', 'system'
    description TEXT,
    properties_schema JSONB,  -- 事件属性的 schema 定义
    owner VARCHAR(50),
    is_core_event BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 行为事件流（核心大表）
CREATE TABLE events (
    event_id BIGINT PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    device_id VARCHAR(100),
    session_id BIGINT,
    event_name VARCHAR(100) REFERENCES event_definitions(event_name),
    event_time TIMESTAMP NOT NULL,
    properties JSONB,  -- 事件自定义属性
    page_name VARCHAR(100),
    referrer VARCHAR(200),
    ip_address VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 会话聚合
CREATE TABLE sessions (
    session_id BIGINT PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    device_id VARCHAR(100),
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds INT,
    event_count INT DEFAULT 0,
    page_view_count INT DEFAULT 0,
    is_bounce BOOLEAN DEFAULT FALSE,  -- 只有1个页面浏览
    entry_page VARCHAR(100),
    exit_page VARCHAR(100),
    traffic_source VARCHAR(50),
    utm_source VARCHAR(50),
    utm_medium VARCHAR(50),
    utm_campaign VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 页面浏览
CREATE TABLE page_views (
    page_view_id BIGINT PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    session_id BIGINT REFERENCES sessions(session_id),
    page_name VARCHAR(100) NOT NULL,
    page_url VARCHAR(500),
    referrer VARCHAR(500),
    duration_seconds INT,
    scroll_depth_pct INT,  -- 0-100
    view_time TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_events_user_id ON events(user_id);
CREATE INDEX idx_events_event_time ON events(event_time);
CREATE INDEX idx_events_event_name ON events(event_name);
CREATE INDEX idx_events_session_id ON events(session_id);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_start_time ON sessions(start_time);
CREATE INDEX idx_page_views_user_id ON page_views(user_id);
CREATE INDEX idx_page_views_session_id ON page_views(session_id);
