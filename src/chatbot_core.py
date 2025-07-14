"""
聊天机器人核心业务逻辑模块
包含餐厅预订对话的主要业务逻辑
"""
import logging
from typing import List, Dict, Any

from .ai_clients import ask_gpt
from .logger import get_reflection_logger


class RestaurantBookingBot:
    """餐厅预订聊天机器人"""
    
    def __init__(self):
        self.reflection_logger = get_reflection_logger()
    
    def check_if_more_info_needed(self, user_input: str, conversation_history: str) -> str:
        """检查是否需要更多信息完成预订"""
        messages = [
            {
                "role": "system", 
                "content": "你是一个餐厅预定助手。请分析用户输入和对话历史，判断是否还需要更多信息才能完成预定。如果需要更多信息，请明确指出缺少什么信息；如果信息足够，请严格回复四个字'信息完整'。特别注意：联系方式是完成预定的必要信息，无论如何都要确保已收集到联系方式。"
            },
            {
                "role": "user", 
                "content": f"用户输入: {user_input}\n对话历史: {conversation_history}\n请判断是否需要更多信息。"
            }
        ]
        return ask_gpt(messages)
    
    def generate_ai_question_for_user(self, missing_info: str, conversation_history: str) -> str:
        """为用户生成追问问题"""
        reflection = self.reflection_logger.get_latest_reflection()
        prompt = f"你是餐厅预定助手。请根据以下缺失信息，友好地向用户追问：{missing_info}。不要重复询问已经问过的问题，请根据对话历史判断哪些信息已提供。严禁使用表情符号、拟人化、情绪化、客套语或任何不专业的表达。"
        
        if reflection:
            prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"对话历史: {conversation_history}"}
        ]
        return ask_gpt(messages)
    
    def generate_ai_message_for_merchant(self, user_input: str, conversation_history: str) -> str:
        """生成发送给商家的消息"""
        reflection = self.reflection_logger.get_latest_reflection()
        prompt = (
            "你是餐厅预定助手。请以用户的身份，用最简洁、直接、事实、专业的口吻，向商家转述用户的完整预定需求。"
            "只传递用户核心意图和关键信息，严禁任何闲聊、主观感受、拟人化表达、多余修饰或表情符号。"
            "使用第一人称'我'或'我们'进行表达，例如：'我需要预定...'，'我们有5人...'。"
            "重要：向商家提供完整的联系电话号码，不要隐藏任何数字，商家需要完整号码进行预定确认。"
        )
        
        if reflection:
            prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"用户输入: {user_input}\n对话历史: {conversation_history}"}
        ]
        return ask_gpt(messages)
    
    def generate_ai_message_for_user_from_merchant(self, merchant_input: str, conversation_history: str, is_final: bool = False) -> str:
        """生成从商家回复转发给用户的消息"""
        reflection = self.reflection_logger.get_latest_reflection()
        
        if is_final:
            prompt = (
                "你是餐厅预定助手。请用自然、友好、专业的客户服务口吻，为用户总结本次预定的最终结果。"
                "总结内容需清晰、简洁，只包含预定核心信息（如最终时间、人数、餐厅、联系方式、特殊需求等）。"
                "严格避免询问用户额外需求、提供菜单推荐等额外服务。只做预定结果的告知。"
                "严禁使用表情符号、拟人化、情绪化、客套语或任何不专业的表达。"
                "不要出现'商家回复:'等字样，也不要暴露AI身份。"
            )
        else:
            prompt = (
                "你是餐厅预定助手。请用自然、友好、专业的客户服务口吻，"
                "根据商家最新回复内容，向用户转述商家的最新回复，并引导用户做出下一步决策（如确认、修改、补充信息等）。"
                "严禁使用表情符号、拟人化、情绪化、客套语或任何不专业的表达。"
                "不要出现'商家回复:'等字样，也不要暴露AI身份。"
                "不要重复用户原始需求，也不要复述商家的话。"
            )
        
        if reflection:
            prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"商家回复: {merchant_input}\n对话历史: {conversation_history}"}
        ]
        return ask_gpt(messages)
    
    def generate_ai_message_for_merchant_from_user(self, user_input: str, conversation_history: str) -> str:
        """生成从用户回复转发给商家的消息"""
        reflection = self.reflection_logger.get_latest_reflection()
        prompt = (
            "你是餐厅预定助手。请以用户的身份，用最简洁、直接、事实、专业的口吻，将用户的最新决策或补充信息转述给商家。"
            "只传递用户核心意图和关键信息，严禁任何闲聊、主观感受、拟人化表达、多余修饰或表情符号。"
            '例如："我同意七点的座位，请帮忙确认预定。"或"我的联系电话是13812345678，请用此号确认预定。"'
            "使用第一人称'我'或'我们'进行表达。"
            "重要：如果涉及联系电话，必须提供完整的号码，不要隐藏任何数字，商家需要完整号码进行预定确认。"
        )
        
        if reflection:
            prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"用户最新输入: {user_input}\n对话历史: {conversation_history}"}
        ]
        return ask_gpt(messages)
    
    def classify_merchant_reply(self, merchant_input: str, conversation_history: str) -> str:
        """分类商家回复类型"""
        system_prompt = (
            "你是一个对话分类助手。请根据商家的回复和对话历史，判断当前回复的类型，只能从以下标签中选择一个并严格只输出标签本身：\n"
            "waiting（等待/处理中/请稍等）、success（预定成功/结束）、need_user（需要用户补充/确认/选择/决策）、continue（继续对话/其他）。\n"
            "特别注意：如果商家回复中包含'只有xx时间'、'只有xx座位'、'是否可以'、'能否接受'、'要不要'、'是否需要'、'是否可以接受'等，或需要用户做出选择、确认、补充时，都应判定为 need_user。"
        )
        user_prompt = f"商家回复: {merchant_input}\n对话历史: {conversation_history}"
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        result = ask_gpt(messages)
        return result.strip().lower()
    
    def identify_missing_info_type(self, merchant_input: str, conversation_history: str) -> str:
        """识别商家需要的缺失信息类型"""
        prompt = (
            "你是一个信息提取助手。商家在回复中表示需要用户补充或确认信息。"
            "请根据以下商家回复和对话历史，判断商家具体需要用户提供什么信息（例如：'联系方式'、'是否接受新时间'、'特殊需求'、'是否确认'等）。"
            "请严格只输出信息类型，不要包含解释或多余的话。"
        )
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"商家回复: {merchant_input}\n对话历史: {conversation_history}"}
        ]
        return ask_gpt(messages)
    
    def check_if_info_already_provided_by_user(self, info_type_summary: str, conversation_history: str) -> str:
        """检查信息是否已在历史对话中提供"""
        prompt = (
            "你是一个信息核对助手。商家提出了一个请求，例如需要'联系方式'或'是否接受7点'。"
            "请检查以下对话历史中，用户是否已经提供了这个信息。"
            "如果用户已提供，请严格只输出'已提供'。"
            "如果用户未提供，请严格只输出'未提供'。"
            "商家请求的信息类型: " + info_type_summary + "\n"
            "对话历史: " + conversation_history
        )
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请判断用户是否已提供此信息。"}
        ]
        return ask_gpt(messages)
    
    def extract_actual_info_value_from_history(self, info_type_summary: str, conversation_history: str) -> str:
        """从历史对话中提取具体信息值"""
        prompt = (
            "你是一个信息提取助手。根据用户对话历史，请提取以下信息类型对应的具体值。"
            "请严格只输出提取到的值，不要包含解释或多余的话。如果找不到，请输出'找不到'。"
            f"信息类型: {info_type_summary}\n"
            f"对话历史: {conversation_history}"
        )
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请提取用户提供的具体值。"}
        ]
        return ask_gpt(messages)
    
    def reflect_on_conversation(self, conversation_log: str) -> str:
        """对整个对话进行AI自我反思"""
        prompt = (
            "你是一个AI自我反思助手。请根据以下完整的对话日志，"
            "深入反思AI在整个对话流程中的表现，指出存在的问题或可以改进的地方，并给出具体改进建议。"
            "请务必将反思的焦点限定在AI自身（你）的言行和决策上，不要去评价用户或商家的行为。"
            "特别是要考虑AI是否能更有效地引导对话、提供信息、或处理特殊情况。"
            "完整对话日志如下：\n" + conversation_log
        )
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请根据以上对话日志反思AI的表现。"}
        ]
        return ask_gpt(messages)
    
    def refine_reflections(self, all_reflections: List[str]) -> str:
        """精炼多条反思记录为一条"""
        prompt = (
            "你是AI反思总结助手。请将以下多条AI自我反思与改进建议进行归纳、去重、精炼，合并为一条最有用、最具指导性的反思建议，便于后续prompt改进：\n"
            + "\n\n".join(all_reflections)
        )
        
        messages = [{"role": "system", "content": prompt}]
        return ask_gpt(messages)


def get_chatbot() -> RestaurantBookingBot:
    """获取聊天机器人实例"""
    return RestaurantBookingBot() 