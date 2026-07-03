-- ============================================
-- Marketing Domain (运营域) - 5 tables
-- ============================================

-- 运营活动
CREATE TABLE campaigns (
    campaign_id SERIAL PRIMARY KEY,
    campaign_name VARCHAR(200) NOT NULL,
    campaign_type VARCHAR(50),  -- 'promotion', 'festival', 'new_user', 'recall'
    description TEXT,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    target_segment_ids INT[],  -- 目标用户分群
    budget DECIMAL(12,2),
    status VARCHAR(20) DEFAULT 'draft',  -- 'draft', 'scheduled', 'active', 'ended'
    owner VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 推送记录
CREATE TABLE push_notifications (
    push_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    campaign_id INT REFERENCES campaigns(campaign_id),
    push_type VARCHAR(50),  -- 'marketing', 'transactional', 'system'
    title VARCHAR(200),
    content TEXT,
    deep_link VARCHAR(500),
    scheduled_at TIMESTAMP,
    sent_at TIMESTAMP,
    delivered_at TIMESTAMP,
    opened_at TIMESTAMP,
    is_delivered BOOLEAN DEFAULT FALSE,
    is_opened BOOLEAN DEFAULT FALSE,
    failure_reason VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 优惠券定义
CREATE TABLE coupons (
    coupon_id SERIAL PRIMARY KEY,
    coupon_code VARCHAR(50) UNIQUE,
    coupon_name VARCHAR(200) NOT NULL,
    coupon_type VARCHAR(50),  -- 'fixed_amount', 'percentage', 'free_shipping'
    discount_value DECIMAL(10,2),  -- 金额或百分比
    min_purchase DECIMAL(10,2),  -- 最低消费门槛
    max_discount DECIMAL(10,2),  -- 最大优惠金额（百分比券用）
    valid_days INT,  -- 领取后有效天数
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    total_quota INT,  -- 总发放量
    per_user_limit INT DEFAULT 1,  -- 每人限领
    applicable_products JSONB,  -- 适用商品/分类
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户领券记录
CREATE TABLE user_coupons (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    coupon_id INT REFERENCES coupons(coupon_id),
    coupon_code VARCHAR(50),
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expire_at TIMESTAMP,
    used_at TIMESTAMP,
    order_id BIGINT,  -- 使用时关联的订单
    status VARCHAR(20) DEFAULT 'available',  -- 'available', 'used', 'expired'
    source VARCHAR(50)  -- 'campaign', 'share', 'purchase', 'new_user'
);

-- Banner/资源位配置
CREATE TABLE banners (
    banner_id SERIAL PRIMARY KEY,
    position VARCHAR(50) NOT NULL,  -- 'home_top', 'home_middle', 'category_top'
    banner_name VARCHAR(200),
    image_url VARCHAR(500),
    target_url VARCHAR(500),
    target_type VARCHAR(50),  -- 'product', 'category', 'campaign', 'external'
    target_id VARCHAR(100),
    sort_order INT DEFAULT 0,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    click_count INT DEFAULT 0,
    impression_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_campaigns_status ON campaigns(status);
CREATE INDEX idx_campaigns_dates ON campaigns(start_date, end_date);
CREATE INDEX idx_push_notifications_user ON push_notifications(user_id);
CREATE INDEX idx_push_notifications_campaign ON push_notifications(campaign_id);
CREATE INDEX idx_push_notifications_sent ON push_notifications(sent_at);
CREATE INDEX idx_coupons_status ON coupons(status);
CREATE INDEX idx_user_coupons_user ON user_coupons(user_id);
CREATE INDEX idx_user_coupons_status ON user_coupons(status);
CREATE INDEX idx_banners_position ON banners(position);
CREATE INDEX idx_banners_active ON banners(is_active, start_date, end_date);
