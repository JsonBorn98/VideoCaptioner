import { defineConfig } from 'vitepress'

const siteUrl = 'https://jsonborn98.github.io/VideoCaptioner/'

export default defineConfig({
  title: 'VideoCaptioner',
  description: '视频字幕工作台：转录与时间轴对齐、双角色 LLM 翻译校对、字幕后处理和 FFmpeg 合成导出',
  titleTemplate: ':title - VideoCaptioner',
  base: '/VideoCaptioner/',

  lastUpdated: true,
  cleanUrls: true,
  ignoreDeadLinks: false,

  transformHead({ pageData }) {
    const canonicalUrl = `${siteUrl}${pageData.relativePath}`
      .replace(/index\.md$/, '')
      .replace(/\.md$/, '')

    return [
      ['link', { rel: 'canonical', href: canonicalUrl }]
    ]
  },

  head: [
    ['link', { rel: 'icon', type: 'image/png', sizes: '32x32', href: `${siteUrl}logo.png` }],
    ['link', { rel: 'apple-touch-icon', sizes: '180x180', href: `${siteUrl}logo.png` }],
    ['meta', { name: 'theme-color', content: '#10b981' }],
    ['meta', { name: 'viewport', content: 'width=device-width, initial-scale=1.0, viewport-fit=cover' }],
    ['meta', {
      name: 'keywords',
      content: 'VideoCaptioner,视频字幕,MiMo ASR,Qwen3 ASR,ForcedAligner,LLM 翻译,字幕校对,字幕后处理,FFmpeg,硬件编码'
    }],
    ['meta', { name: 'author', content: 'JsonBorn98 and VideoCaptioner contributors' }],
    ['meta', {
      name: 'robots',
      content: 'index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1'
    }],

    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:locale', content: 'zh_CN' }],
    ['meta', { property: 'og:title', content: 'VideoCaptioner - 视频字幕工作台' }],
    ['meta', {
      property: 'og:description',
      content: '集成转录与精确时间轴、双角色 LLM 翻译校对、字幕后处理和可控 FFmpeg 合成导出。'
    }],
    ['meta', { property: 'og:site_name', content: 'VideoCaptioner' }],
    ['meta', { property: 'og:url', content: siteUrl }],
    ['meta', { property: 'og:image', content: `${siteUrl}main.png` }],
    ['meta', { property: 'og:image:alt', content: 'VideoCaptioner 界面预览' }],

    ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
    ['meta', { name: 'twitter:title', content: 'VideoCaptioner - Video Subtitle Workbench' }],
    ['meta', {
      name: 'twitter:description',
      content: 'Transcription and forced alignment, dual-role LLM translation review, subtitle postprocessing, and controllable FFmpeg export.'
    }],
    ['meta', { name: 'twitter:image', content: `${siteUrl}main.png` }],

    ['script', { type: 'application/ld+json' }, JSON.stringify({
      '@context': 'https://schema.org',
      '@graph': [
        {
          '@type': 'SoftwareApplication',
          '@id': `${siteUrl}#software`,
          name: 'VideoCaptioner',
          alternateName: ['Video Captioner', 'Video Subtitle Workbench'],
          description: '视频字幕工作台，支持转录与时间轴对齐、翻译校对、字幕后处理和视频合成。',
          applicationCategory: 'MultimediaApplication',
          operatingSystem: ['Windows', 'macOS', 'Linux'],
          author: {
            '@type': 'Organization',
            name: 'VideoCaptioner contributors',
            url: 'https://github.com/JsonBorn98/VideoCaptioner'
          },
          screenshot: `${siteUrl}main.png`,
          url: siteUrl,
          image: `${siteUrl}logo.png`,
          inLanguage: ['zh-CN', 'en-US'],
          featureList: [
            'MiMo and Qwen3 ASR',
            'Qwen3 ForcedAligner timeline alignment',
            'Dual-role LLM translation and review',
            'Adaptive subtitle postprocessing',
            'FFmpeg hardware-accelerated synthesis'
          ]
        },
        {
          '@type': 'WebSite',
          '@id': `${siteUrl}#website`,
          url: siteUrl,
          name: 'VideoCaptioner Documentation',
          description: 'VideoCaptioner fork 用户文档',
          publisher: {
            '@id': `${siteUrl}#project`
          },
          inLanguage: ['zh-CN', 'en-US']
        },
        {
          '@type': 'Organization',
          '@id': `${siteUrl}#project`,
          name: 'VideoCaptioner contributors',
          url: 'https://github.com/JsonBorn98/VideoCaptioner',
          logo: {
            '@type': 'ImageObject',
            url: `${siteUrl}logo.png`
          }
        }
      ]
    })]
  ],

  sitemap: {
    hostname: siteUrl
  },

  themeConfig: {
    logo: '/logo.png',

    nav: [
      { text: '首页', link: '/' },
      { text: '快速开始', link: '/guide/getting-started' },
      { text: '工作流程', link: '/guide/workflow' },
      { text: 'CLI', link: '/cli' },
      { text: 'GitHub', link: 'https://github.com/JsonBorn98/VideoCaptioner' }
    ],

    sidebar: {
      '/guide/': [
        {
          text: '使用指南',
          items: [
            { text: '快速开始', link: '/guide/getting-started' },
            { text: '工作流程', link: '/guide/workflow' },
            { text: '字幕后处理', link: '/guide/subtitle-postprocessing' },
            { text: 'FFmpeg 合成导出', link: '/guide/video-synthesis' },
            { text: '字幕样式', link: '/guide/subtitle-style' },
            { text: 'Cookie 配置', link: '/guide/cookies-config' },
            { text: '常见问题', link: '/guide/faq' }
          ]
        }
      ],
      '/config/': [
        {
          text: '配置指南',
          items: [
            { text: '语音识别与对齐', link: '/config/asr' },
            { text: 'LLM 模型方案', link: '/config/llm' },
            { text: '翻译模式与校对', link: '/config/translator' },
            { text: 'Cookie 配置', link: '/config/cookies' }
          ]
        }
      ],
      '/dev/': [
        {
          text: '开发文档',
          items: [
            { text: '贡献指南', link: '/dev/contributing' }
          ]
        }
      ]
    },

    search: {
      provider: 'local',
      options: {
        locales: {
          root: {
            translations: {
              button: {
                buttonText: '搜索文档',
                buttonAriaLabel: '搜索文档'
              },
              modal: {
                noResultsText: '无法找到相关结果',
                resetButtonTitle: '清除查询条件',
                footer: {
                  selectText: '选择',
                  navigateText: '切换'
                }
              }
            }
          }
        }
      }
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/JsonBorn98/VideoCaptioner' }
    ],

    editLink: {
      pattern: 'https://github.com/JsonBorn98/VideoCaptioner/edit/master/docs/:path',
      text: '在 GitHub 上编辑此页'
    },

    footer: {
      message: '基于 GPL-3.0 许可发布',
      copyright: 'Copyright © 2026 JsonBorn98 and VideoCaptioner contributors'
    },

    docFooter: {
      prev: '上一页',
      next: '下一页'
    },

    outline: {
      label: '页面导航'
    },

    lastUpdated: {
      text: '最后更新于',
      formatOptions: {
        dateStyle: 'short',
        timeStyle: 'medium'
      }
    },

    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '菜单',
    darkModeSwitchLabel: '主题',
    lightModeSwitchTitle: '切换到浅色模式',
    darkModeSwitchTitle: '切换到深色模式'
  }
})
