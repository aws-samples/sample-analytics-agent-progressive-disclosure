-- ============================================
-- Transaction Domain (交易域) - 4 tables
-- ============================================

-- 订单主表
CREATE TABLE orders (
    order_id BIGSERIAL PRIMARY KEY,
    order_no VARCHAR(50) UNIQUE NOT NULL,
    user_id BIGINT REFERENCES users(user_id),
    status VARCHAR(30) NOT NULL,  -- 'pending', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded'
    total_amount DECIMAL(12,2) NOT NULL,
    discount_amount DECIMAL(12,2) DEFAULT 0,
    shipping_fee DECIMAL(10,2) DEFAULT 0,
    actual_amount DECIMAL(12,2) NOT NULL,  -- 实付金额
    item_count INT DEFAULT 0,
    coupon_id INT REFERENCES coupons(coupon_id),
    shipping_address JSONB,
    remark TEXT,
    placed_at TIMESTAMP NOT NULL,  -- 下单时间
    paid_at TIMESTAMP,
    shipped_at TIMESTAMP,
    delivered_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    cancel_reason VARCHAR(200),
    refunded_at TIMESTAMP,
    refund_reason VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 订单明细
CREATE TABLE order_items (
    item_id BIGSERIAL PRIMARY KEY,
    order_id BIGINT REFERENCES orders(order_id),
    product_id BIGINT,  -- REFERENCES products(product_id)
    product_name VARCHAR(200),
    sku_id BIGINT,
    sku_name VARCHAR(200),
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount_amount DECIMAL(10,2) DEFAULT 0,
    actual_amount DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 支付记录
CREATE TABLE payments (
    payment_id BIGSERIAL PRIMARY KEY,
    payment_no VARCHAR(50) UNIQUE NOT NULL,
    order_id BIGINT REFERENCES orders(order_id),
    user_id BIGINT REFERENCES users(user_id),
    amount DECIMAL(12,2) NOT NULL,
    payment_method VARCHAR(50),  -- 'wechat', 'alipay', 'credit_card', 'balance'
    payment_channel VARCHAR(50),  -- 'app', 'h5', 'mini_program'
    status VARCHAR(20) NOT NULL,  -- 'pending', 'success', 'failed', 'refunded'
    transaction_id VARCHAR(100),  -- 第三方支付流水号
    paid_at TIMESTAMP,
    failure_reason VARCHAR(200),
    refund_amount DECIMAL(12,2) DEFAULT 0,
    refunded_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 会员订阅
CREATE TABLE subscriptions (
    subscription_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    plan_name VARCHAR(100) NOT NULL,  -- 'monthly', 'quarterly', 'yearly'
    plan_price DECIMAL(10,2),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    auto_renew BOOLEAN DEFAULT TRUE,
    status VARCHAR(20) DEFAULT 'active',  -- 'active', 'expired', 'cancelled'
    payment_id BIGINT REFERENCES payments(payment_id),
    cancelled_at TIMESTAMP,
    cancel_reason VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_placed_at ON orders(placed_at);
CREATE INDEX idx_orders_paid_at ON orders(paid_at);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_payments_order ON payments(order_id);
CREATE INDEX idx_payments_user ON payments(user_id);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_subscriptions_user ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
