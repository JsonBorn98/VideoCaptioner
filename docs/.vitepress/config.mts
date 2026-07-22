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
    ['meta', { name: 'viewport', content: 'width=device-width, initial-scale=1.0, viewport-fit=cover' }]
  ],

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
            { text: '开发说明', link: '/dev/contributing' }
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

    footer: {
      message: '个人使用向 fork · GPL-3.0'
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
