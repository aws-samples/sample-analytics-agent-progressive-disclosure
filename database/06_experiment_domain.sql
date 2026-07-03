-- ============================================
-- Experiment Domain (实验域) - 3 tables
-- ============================================

-- A/B 测试配置
CREATE TABLE ab_tests (
    test_id SERIAL PRIMARY KEY,
    test_name VARCHAR(200) NOT NULL,
    test_key VARCHAR(100) UNIQUE NOT NULL,  -- 代码中引用的 key
    hypothesis TEXT,
    description TEXT,
    primary_metric VARCHAR(100),  -- 'conversion_rate', 'retention_d7', 'revenue_per_user'
    secondary_metrics TEXT[],
    target_segment_ids INT[],  -- 目标用户群
    traffic_percentage INT DEFAULT 100,  -- 整体流量占比
    min_sample_size INT,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'draft',  -- 'draft', 'running', 'paused', 'concluded'
    conclusion TEXT,
    winner_variant_id INT,
    owner VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 实验分支
CREATE TABLE ab_test_variants (
    variant_id SERIAL PRIMARY KEY,
    test_id INT REFERENCES ab_tests(test_id),
    variant_name VARCHAR(100) NOT NULL,
    variant_key VARCHAR(50) NOT NULL,  -- 'control', 'treatment_a', 'treatment_b'
    description TEXT,
    traffic_percentage INT NOT NULL,  -- 该分支的流量占比
    config_json JSONB,  -- 分支的具体配置
    is_control BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(test_id, variant_key)
);

-- 用户分组
CREATE TABLE ab_test_assignments (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    test_id INT REFERENCES ab_tests(test_id),
    variant_id INT REFERENCES ab_test_variants(variant_id),
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    first_exposure_at TIMESTAMP,  -- 首次曝光时间
    UNIQUE(user_id, test_id)
);

-- Indexes
CREATE INDEX idx_ab_tests_status ON ab_tests(status);
CREATE INDEX idx_ab_tests_key ON ab_tests(test_key);
CREATE INDEX idx_ab_test_variants_test ON ab_test_variants(test_id);
CREATE INDEX idx_ab_test_assignments_user ON ab_test_assignments(user_id);
CREATE INDEX idx_ab_test_assignments_test ON ab_test_assignments(test_id);
CREATE INDEX idx_ab_test_assignments_variant ON ab_test_assignments(variant_id);
