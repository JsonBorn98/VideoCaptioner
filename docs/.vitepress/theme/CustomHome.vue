<template>
  <div class="custom-home">
    <!-- Hero Section -->
    <section class="hero-section">
      <div class="hero-bg-decoration"></div>
      <div class="container">
        <div class="hero-content">
          <div class="hero-badge">
            <span class="badge-emoji">🎬</span>
            Fork 独立维护 · GPL-3.0
          </div>
          <h1 class="hero-title">
            从转录到成片<br>
            <span class="gradient-text">每一步都可控</span>
          </h1>
          <p class="hero-description">
            精确时间轴、双角色翻译校对、字幕后处理与专业编码<br>
            GUI 与 CLI 共享核心流程，并分别服务交互精修与批量自动化
          </p>
          <div class="hero-stats-inline">
            <span class="stat-inline">🎙️ MiMo / Qwen / Whisper</span>
            <span class="stat-divider">·</span>
            <span class="stat-inline">🌐 双角色 LLM 翻译</span>
            <span class="stat-divider">·</span>
            <span class="stat-inline">🎞️ 14 个编码器目录项</span>
          </div>
          <div class="hero-actions">
            <a :href="withBase('/guide/getting-started')" class="btn-primary">
              <span>立即开始</span>
              <span class="btn-arrow">→</span>
            </a>
            <a href="https://github.com/JsonBorn98/VideoCaptioner" class="btn-secondary">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.603-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.463-1.11-1.463-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.114 2.504.336 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z"/>
              </svg>
              <span>看看源码</span>
            </a>
          </div>
        </div>
        <div class="hero-visual">
          <div class="visual-card">
            <div class="card-decoration"></div>
            <img :src="withBase('/main.png')" alt="VideoCaptioner 界面预览">
            <div class="card-badge">字幕工作台</div>
          </div>
        </div>
      </div>
    </section>

    <!-- Stats Section -->
    <section class="stats-section">
      <div class="container">
        <div class="stats-grid">
          <div class="stat-item">
            <div class="stat-number">3</div>
            <div class="stat-label">翻译工作模式</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">14</div>
            <div class="stat-label">编码器目录项</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">SRT/VTT/ASS</div>
            <div class="stat-label">字幕输入与导出</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">GUI + CLI</div>
            <div class="stat-label">桌面与自动化入口</div>
          </div>
        </div>
      </div>
    </section>

    <!-- Features Section -->
    <section class="features-section">
      <div class="container">
        <div class="section-header">
          <h2 class="section-title">Fork 核心增强</h2>
          <p class="section-subtitle">围绕字幕生产链路补齐可控性、质量保障和交付能力</p>
        </div>
        <div class="features-grid">
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">🎙️</div>
            </div>
            <h3 class="feature-title">专业转录与时间轴</h3>
            <p class="feature-desc">MiMo API、Qwen3 本地 ASR 和 Qwen3-ForcedAligner，配合 VAD 分块、常驻 Worker、异常重试和降级时间轴。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">🌐</div>
            </div>
            <h3 class="feature-title">双角色翻译校对</h3>
            <p class="feature-desc">主翻译与高级校对分别负责全文分析、术语裁决、分批翻译和完整审计，并生成可复用术语表。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">📐</div>
            </div>
            <h3 class="feature-title">自适应字幕后处理</h3>
            <p class="feature-desc">处理闪轴、单句时长、阅读速度、标点、重叠和尾部补偿，支持 QA 报告与可验证的局部语义修复。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">🎞️</div>
            </div>
            <h3 class="feature-title">可控 FFmpeg 合成</h3>
            <p class="feature-desc">软件与硬件编码、CQ/ABR、画面与音频参数、命令预览、自定义参数、实时 Console 和任务控制集中管理。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">📋</div>
            </div>
            <h3 class="feature-title">阶段摘要与运行日志</h3>
            <p class="feature-desc">GUI 与 CLI 展示阶段产物、降级状态和实时日志，长任务中的失败与回退都有据可查。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon-wrap">
              <div class="feature-icon">🎨</div>
            </div>
            <h3 class="feature-title">双语样式与阶段交付</h3>
            <p class="feature-desc">保持单双语布局，提供扩展 ASS 样式与字体预设，并为转录、初版和后处理阶段保存规范 SRT 或自动导出 ASS/VTT。</p>
          </div>
        </div>
      </div>
    </section>

    <!-- CTA Section -->
    <section class="cta-section">
      <div class="container">
        <div class="cta-content">
          <div class="cta-emoji">🚀</div>
          <h2 class="cta-title">从源码开始使用</h2>
          <p class="cta-description">
            当前 fork 尚未发布公开桌面 Release<br>
            使用 uv 安装主程序，再按需启用本地模型或云端服务
          </p>
          <div class="cta-buttons">
            <a :href="withBase('/guide/getting-started')" class="btn-cta-primary">
              <span>开始使用</span>
              <span class="btn-arrow">→</span>
            </a>
            <a :href="withBase('/guide/faq')" class="btn-cta-secondary">
              <span>查看常见问题</span>
            </a>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { withBase } from 'vitepress'
</script>

<style scoped>
/* 绿色系配色 - 高对比度 */
.custom-home {
  --vc-green-primary: #10b981;
  --vc-green-dark: #047857;
  --vc-green-light: #6ee7b7;
  --vc-text-strong: #1a1a1a;
  --vc-text-body: #4b5563;
  --vc-bg-soft: #f9fafb;
}

.dark .custom-home {
  --vc-text-strong: #ffffff;
  --vc-text-body: #d1d5db;
  --vc-bg-soft: #1f2937;
  --vc-green-primary: #34d399;
  --vc-green-dark: #10b981;
}

/* Container */
.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 2rem;
}

/* Hero Section */
.hero-section {
  position: relative;
  padding: 6rem 0 4rem;
  overflow: hidden;
  background: var(--vp-c-bg);
}

.hero-bg-decoration {
  position: absolute;
  top: -50%;
  right: -20%;
  width: 800px;
  height: 800px;
  background: radial-gradient(circle, rgba(16, 185, 129, 0.05) 0%, transparent 70%);
  border-radius: 50%;
  pointer-events: none;
}

.hero-section .container {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4rem;
  align-items: center;
  position: relative;
  z-index: 1;
}

.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 1rem;
  background: var(--vc-bg-soft);
  color: var(--vc-green-dark);
  border-radius: 100px;
  font-size: 0.875rem;
  font-weight: 600;
  margin-bottom: 2rem;
  border: 1.5px solid var(--vc-green-primary);
}

.dark .hero-badge {
  background: rgba(16, 185, 129, 0.15);
  color: var(--vc-green-light);
  border-color: var(--vc-green-primary);
}

.badge-emoji {
  font-size: 1.1rem;
}

.hero-title {
  font-size: clamp(2.5rem, 5vw, 4rem);
  font-weight: 800;
  line-height: 1.15;
  letter-spacing: -0.02em;
  margin: 0 0 1.5rem;
  color: var(--vc-text-strong);
}

.gradient-text {
  color: var(--vc-green-primary);
}

.hero-description {
  font-size: 1.15rem;
  line-height: 1.8;
  color: var(--vc-text-body);
  margin: 0 0 1.5rem;
}

.hero-stats-inline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin: 0 0 2rem;
  font-size: 0.9375rem;
  color: var(--vc-text-body);
}

.stat-inline {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.375rem 0.75rem;
  background: var(--vc-bg-soft);
  border-radius: 8px;
}

.stat-divider {
  opacity: 0.3;
}

.hero-actions {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
}

.btn-primary, .btn-secondary {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.875rem 1.75rem;
  font-size: 1rem;
  font-weight: 600;
  border-radius: 8px;
  text-decoration: none;
  transition: all 0.2s ease;
}

.btn-primary {
  background: var(--vc-green-primary);
  color: white;
  border: none;
}

.btn-primary:hover {
  background: var(--vc-green-dark);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
}

.btn-arrow {
  transition: transform 0.2s ease;
}

.btn-primary:hover .btn-arrow {
  transform: translateX(2px);
}

.btn-secondary {
  background: var(--vp-c-bg);
  color: var(--vc-text-body);
  border: 1.5px solid var(--vp-c-divider);
}

.btn-secondary:hover {
  border-color: var(--vc-green-primary);
  color: var(--vc-green-primary);
  background: var(--vc-bg-soft);
}

.hero-visual {
  position: relative;
}

.visual-card {
  position: relative;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.12);
  background: var(--vp-c-bg);
  border: 1px solid var(--vp-c-divider);
}

.card-decoration {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: linear-gradient(90deg, var(--vc-green-1), var(--vc-green-3));
}

.visual-card img {
  width: 100%;
  height: auto;
  display: block;
}

.card-badge {
  position: absolute;
  top: 1rem;
  right: 1rem;
  padding: 0.375rem 0.75rem;
  background: rgba(16, 185, 129, 0.9);
  backdrop-filter: blur(8px);
  color: white;
  font-size: 0.75rem;
  font-weight: 600;
  border-radius: 6px;
}

/* Stats Section */
.stats-section {
  padding: 3rem 0;
  border-top: 1px solid var(--vp-c-divider);
  border-bottom: 1px solid var(--vp-c-divider);
  background: var(--vc-bg-soft);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 2rem;
}

.stat-item {
  text-align: center;
}

.stat-number {
  font-size: 2.5rem;
  font-weight: 800;
  color: var(--vc-green-primary);
  line-height: 1;
  margin-bottom: 0.5rem;
}

.stat-label {
  font-size: 0.9375rem;
  color: var(--vc-text-body);
  font-weight: 500;
}

/* Features Section */
.features-section {
  padding: 5rem 0;
  background: var(--vp-c-bg);
}

.section-header {
  text-align: center;
  margin-bottom: 3.5rem;
}

.section-title {
  font-size: clamp(2rem, 4vw, 2.75rem);
  font-weight: 800;
  line-height: 1.2;
  letter-spacing: -0.02em;
  margin: 0 0 0.75rem;
  color: var(--vc-text-strong);
}

.section-subtitle {
  font-size: 1.125rem;
  color: var(--vc-text-body);
  margin: 0;
}

.features-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 2rem;
}

.feature-card {
  padding: 2rem;
  background: var(--vc-bg-soft);
  border-radius: 12px;
  border: 1px solid var(--vp-c-divider);
  transition: all 0.2s ease;
}

.feature-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 16px rgba(0, 0, 0, 0.08);
  border-color: var(--vc-green-primary);
}

.feature-icon-wrap {
  display: inline-flex;
  padding: 0.75rem;
  background: rgba(16, 185, 129, 0.1);
  border-radius: 10px;
  margin-bottom: 1.25rem;
}

.feature-icon {
  font-size: 2rem;
  line-height: 1;
}

.feature-title {
  font-size: 1.25rem;
  font-weight: 700;
  margin: 0 0 0.75rem;
  color: var(--vc-text-strong);
}

.feature-desc {
  font-size: 0.9375rem;
  line-height: 1.7;
  color: var(--vc-text-body);
  margin: 0;
}

/* CTA Section */
.cta-section {
  padding: 5rem 0;
  background: linear-gradient(135deg, var(--vc-green-primary) 0%, var(--vc-green-dark) 100%);
  color: white;
  position: relative;
  overflow: hidden;
}

.cta-section::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -20%;
  width: 600px;
  height: 600px;
  background: radial-gradient(circle, rgba(255, 255, 255, 0.08) 0%, transparent 70%);
  border-radius: 50%;
}

.cta-content {
  text-align: center;
  position: relative;
  z-index: 1;
}

.cta-emoji {
  font-size: 3rem;
  margin-bottom: 1rem;
}

.cta-title {
  font-size: clamp(1.75rem, 4vw, 2.5rem);
  font-weight: 800;
  margin: 0 0 1rem;
  letter-spacing: -0.01em;
  color: white;
}

.cta-description {
  font-size: 1.125rem;
  margin: 0 0 2rem;
  opacity: 0.95;
  line-height: 1.7;
  color: rgba(255, 255, 255, 0.9);
}

.cta-buttons {
  display: flex;
  gap: 1rem;
  justify-content: center;
  flex-wrap: wrap;
}

.btn-cta-primary, .btn-cta-secondary {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 1rem 2rem;
  font-size: 1rem;
  font-weight: 600;
  border-radius: 8px;
  text-decoration: none;
  transition: all 0.2s ease;
}

.btn-cta-primary {
  background: white;
  color: var(--vc-green-dark);
  border: none;
}

.btn-cta-primary:hover {
  transform: translateY(-1px);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.15);
}

.btn-cta-primary:hover .btn-arrow {
  transform: translateX(2px);
}

.btn-cta-secondary {
  background: rgba(255, 255, 255, 0.1);
  backdrop-filter: blur(10px);
  color: white;
  border: 1.5px solid rgba(255, 255, 255, 0.3);
}

.btn-cta-secondary:hover {
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.5);
}

/* Responsive */
@media (max-width: 960px) {
  .hero-section .container {
    grid-template-columns: 1fr;
    gap: 3rem;
  }

  .hero-visual {
    order: -1;
  }

  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .features-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .hero-section {
    padding: 4rem 0 3rem;
  }

  .stats-grid {
    grid-template-columns: 1fr;
    gap: 1.5rem;
  }

  .hero-stats-inline {
    flex-direction: column;
    align-items: flex-start;
  }

  .stat-divider {
    display: none;
  }

  .cta-buttons {
    flex-direction: column;
    align-items: stretch;
  }

  .btn-cta-primary, .btn-cta-secondary {
    width: 100%;
    justify-content: center;
  }
}
</style>
