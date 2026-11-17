"""语音对话（docs/05 §8.4）：端侧 KWS 唤醒 + 流式 ASR + TTS 朗读链路。

偏差（已回写 05）：ASR 推理在脑侧（壳只采麦克风 PCM 走环回 WS，音频不出机，隐私等价）；
TTS 用壳侧系统 speechSynthesis（CosyVoice2 体积破 portable 红线）；口型=boundary 事件驱动。
"""
