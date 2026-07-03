-- ============================================
-- Social Domain (社交互动域) - 6 tables
-- ============================================

-- 关注关系
CREATE TABLE user_follows (
    follower_id BIGINT REFERENCES users(user_id),
    following_id BIGINT REFERENCES users(user_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (follower_id, following_id)
);

-- 用户发布内容(UGC)
CREATE TABLE posts (
    post_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    content_type VARCHAR(50) NOT NULL,  -- 'article', 'short_video', 'image', 'review'
    title VARCHAR(200),
    content TEXT,
    media_urls TEXT[],  -- 图片/视频链接
    tags TEXT[],
    location VARCHAR(100),
    product_ids BIGINT[],  -- 关联的商品
    view_count INT DEFAULT 0,
    like_count INT DEFAULT 0,
    comment_count INT DEFAULT 0,
    share_count INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'published',  -- 'draft', 'published', 'hidden', 'deleted'
    is_featured BOOLEAN DEFAULT FALSE,
    published_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 点赞
CREATE TABLE post_likes (
    user_id BIGINT REFERENCES users(user_id),
    post_id BIGINT REFERENCES posts(post_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, post_id)
);

-- 评论
CREATE TABLE post_comments (
    comment_id BIGSERIAL PRIMARY KEY,
    post_id BIGINT REFERENCES posts(post_id),
    user_id BIGINT REFERENCES users(user_id),
    parent_comment_id BIGINT REFERENCES post_comments(comment_id),  -- 支持回复
    content TEXT NOT NULL,
    like_count INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'visible',  -- 'visible', 'hidden', 'deleted'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 分享
CREATE TABLE post_shares (
    share_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id),
    post_id BIGINT REFERENCES posts(post_id),
    share_channel VARCHAR(50),  -- 'wechat_friend', 'wechat_moment', 'weibo', 'copy_link'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 私信
CREATE TABLE user_messages (
    message_id BIGSERIAL PRIMARY KEY,
    sender_id BIGINT REFERENCES users(user_id),
    receiver_id BIGINT REFERENCES users(user_id),
    content TEXT NOT NULL,
    message_type VARCHAR(20) DEFAULT 'text',  -- 'text', 'image', 'product'
    related_post_id BIGINT REFERENCES posts(post_id),
    related_product_id BIGINT,
    is_read BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_at TIMESTAMP
);

-- Indexes
CREATE INDEX idx_user_follows_follower ON user_follows(follower_id);
CREATE INDEX idx_user_follows_following ON user_follows(following_id);
CREATE INDEX idx_posts_user_id ON posts(user_id);
CREATE INDEX idx_posts_published_at ON posts(published_at);
CREATE INDEX idx_posts_content_type ON posts(content_type);
CREATE INDEX idx_post_likes_post ON post_likes(post_id);
CREATE INDEX idx_post_comments_post ON post_comments(post_id);
CREATE INDEX idx_post_comments_user ON post_comments(user_id);
CREATE INDEX idx_post_shares_post ON post_shares(post_id);
CREATE INDEX idx_user_messages_receiver ON user_messages(receiver_id);
CREATE INDEX idx_user_messages_sender ON user_messages(sender_id);
