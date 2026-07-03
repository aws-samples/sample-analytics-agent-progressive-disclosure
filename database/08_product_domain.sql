-- ============================================
-- Product Domain (商品/内容域) - 3 tables
-- ============================================

-- 分类（支持多级）
CREATE TABLE categories (
    category_id SERIAL PRIMARY KEY,
    parent_id INT REFERENCES categories(category_id),
    category_name VARCHAR(100) NOT NULL,
    level INT NOT NULL,  -- 1, 2, 3...
    sort_order INT DEFAULT 0,
    icon_url VARCHAR(500),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 商品/内容
CREATE TABLE products (
    product_id BIGSERIAL PRIMARY KEY,
    product_name VARCHAR(200) NOT NULL,
    category_id INT REFERENCES categories(category_id),
    brand VARCHAR(100),
    description TEXT,
    price DECIMAL(10,2) NOT NULL,
    original_price DECIMAL(10,2),
    cost DECIMAL(10,2),  -- 成本价（用于计算毛利）
    stock INT DEFAULT 0,
    sold_count INT DEFAULT 0,
    view_count INT DEFAULT 0,
    favorite_count INT DEFAULT 0,
    rating_avg DECIMAL(2,1) DEFAULT 0,
    rating_count INT DEFAULT 0,
    main_image_url VARCHAR(500),
    image_urls TEXT[],
    status VARCHAR(20) DEFAULT 'active',  -- 'draft', 'active', 'inactive', 'deleted'
    is_featured BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 商品标签
CREATE TABLE product_tags (
    id SERIAL PRIMARY KEY,
    product_id BIGINT REFERENCES products(product_id),
    tag_name VARCHAR(50) NOT NULL,
    tag_type VARCHAR(30),  -- 'category', 'style', 'scene', 'promotion'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, tag_name)
);

-- Indexes
CREATE INDEX idx_categories_parent ON categories(parent_id);
CREATE INDEX idx_categories_level ON categories(level);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_status ON products(status);
CREATE INDEX idx_products_price ON products(price);
CREATE INDEX idx_product_tags_product ON product_tags(product_id);
CREATE INDEX idx_product_tags_name ON product_tags(tag_name);
