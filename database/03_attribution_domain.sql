-- ============================================
-- Attribution Domain (渠道归因域) - 5 tables
-- ============================================

-- 渠道定义
CREATE TABLE channels (
    channel_id SERIAL PRIMARY KEY,
    channel_name VARCHAR(100) NOT NULL,
    channel_type VARCHAR(50) NOT NULL,  -- 'organic', 'paid', 'kol', 'referral', 'direct'
    platform VARCHAR(50),  -- 'douyin', 'weixin', 'xiaohongshu', 'baidu', etc.
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 广告投放活动
CREATE TABLE ad_campaigns (
    ad_campaign_id SERIAL PRIMARY KEY,
    channel_id INT REFERENCES channels(channel_id),
    campaign_name VARCHAR(200) NOT NULL,
    campaign_type VARCHAR(50),  -- 'awareness', 'acquisition', 'retargeting'
    objective VARCHAR(50),  -- 'installs', 'purchases', 'engagement'
    budget_total DECIMAL(12,2),
    budget_daily DECIMAL(12,2),
    start_date DATE,
    end_date DATE,
    target_audience JSONB,  -- 投放人群定向
    status VARCHAR(20) DEFAULT 'draft',  -- 'draft', 'active', 'paused', 'ended'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 广告素材
CREATE TABLE ad_creatives (
    creative_id SERIAL PRIMARY KEY,
    ad_campaign_id INT REFERENCES ad_campaigns(ad_campaign_id),
    creative_name VARCHAR(200),
    creative_type VARCHAR(50),  -- 'image', 'video', 'carousel', 'text'
    creative_format VARCHAR(50),  -- '1080x1920', '750x1334', etc.
    content_url VARCHAR(500),
    headline VARCHAR(200),
    description TEXT,
    call_to_action VARCHAR(50),
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户归因记录
CREATE TABLE user_attributions (
    attribution_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    channel_id INT REFERENCES channels(channel_id),
    ad_campaign_id INT REFERENCES ad_campaigns(ad_campaign_id),
    creative_id INT REFERENCES ad_creatives(creative_id),
    attribution_type VARCHAR(50),  -- 'first_touch', 'last_touch', 'linear'
    click_time TIMESTAMP,
    install_time TIMESTAMP,
    attributed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    days_to_install INT,
    tracking_params JSONB  -- utm_source, utm_medium, etc.
);

-- 渠道每日成本
CREATE TABLE channel_daily_costs (
    id BIGSERIAL PRIMARY KEY,
    channel_id INT REFERENCES channels(channel_id),
    ad_campaign_id INT REFERENCES ad_campaigns(ad_campaign_id),
    creative_id INT REFERENCES ad_creatives(creative_id),
    date DATE NOT NULL,
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    installs INT DEFAULT 0,
    cost DECIMAL(12,2) DEFAULT 0,
    currency VARCHAR(10) DEFAULT 'CNY',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, ad_campaign_id, creative_id, date)
);

-- Indexes
CREATE INDEX idx_ad_campaigns_channel ON ad_campaigns(channel_id);
CREATE INDEX idx_ad_campaigns_status ON ad_campaigns(status);
CREATE INDEX idx_ad_creatives_campaign ON ad_creatives(ad_campaign_id);
CREATE INDEX idx_user_attributions_user ON user_attributions(user_id);
CREATE INDEX idx_user_attributions_channel ON user_attributions(channel_id);
CREATE INDEX idx_channel_daily_costs_date ON channel_daily_costs(date);
