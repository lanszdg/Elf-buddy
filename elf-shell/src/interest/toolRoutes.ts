export const interestTools = {
  photos: { name: '对照照片', width: 820, height: 675 },
  picks: { name: '我的兴趣清单', width: 640, height: 610 },
  research: { name: '对照资料', width: 690, height: 660 },
  events: { name: '找活动', width: 710, height: 635 },
  handoff: { name: '待续事项', width: 580, height: 595 },
  watch: { name: '关注的版本', width: 635, height: 585 },
  results: { name: '我的成果', width: 650, height: 590 },
  avatar: { name: '编辑精灵形象', width: 690, height: 645 },
  practice: { name: '试试动作', width: 805, height: 640 },
} as const
export type InterestTool = keyof typeof interestTools

export function isInterestTool(value: unknown): value is InterestTool {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(interestTools, value)
}

export function requestedTool(text: string): InterestTool | undefined {
  if (/(对照|对比|比较|看看|分析).{0,9}(照片|两张图)|^选片$/.test(text)) return 'photos'
  if (/对照资料|比较资料|两份资料|比对文章|哪一份资料/.test(text)) return 'research'
  if (/(找|查|搜|看).{0,12}(活动|展览|摄影展|演出)|^活动与行程$/.test(text)) return 'events'
  if (/版本追踪|关注.{0,12}版本|github.{0,14}(更新|发布)|安装包/.test(text.toLowerCase())) return 'watch'
  if (/(找回|查看|导出|我的).{0,8}成果|^导出$|整理作品/.test(text)) return 'results'
  if (/待续|刚才收的|我存的|上次.{0,5}继续/.test(text)) return 'handoff'
  if (/捏脸|换皮肤|(编辑|改|换|捏).{0,6}形象|换外观|换个颜色/.test(text)) return 'avatar'
  if (/手感|动手实验|阻尼|试试动作|调动画/.test(text)) return 'practice'
  if (/兴趣清单|加入清单|收藏列表/.test(text)) return 'picks'
}
