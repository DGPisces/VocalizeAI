"""
餐厅预订聊天机器人主程序
使用模块化设计，支持语音交互和AI自我反思
"""
import logging
from typing import List

from .config import get_config, validate_config
from .logger import setup_logging, get_dialogue_logger, get_reflection_logger
from .ai_clients import get_ai_manager
from .audio import generate_and_play_voice
from .chatbot_core import get_chatbot

class ChatbotApp:
    """聊天机器人应用类"""
    
    def __init__(self):
        # 设置日志
        setup_logging()
        
        # 获取配置
        self.config = get_config()
        
        # 验证配置
        if not validate_config():
            logging.error("配置验证失败，请检查环境变量设置")
            return
        
        # 初始化组件
        self.dialogue_logger = get_dialogue_logger()
        self.reflection_logger = get_reflection_logger()
        self.chatbot = get_chatbot()
        self.ai_manager = get_ai_manager()
        
        logging.info("聊天机器人应用初始化完成")
    
    def run(self) -> None:
        """运行聊天机器人主程序"""
        try:
            # 清空对话日志开始新会话
            self.dialogue_logger.clear_log()
            
            print("=== 语音预定订餐系统 ===")
            conversation_history = []
            
            # 用户输入初始需求
            user_input = input("用户，请输入你的预定需求：\n> ")
            conversation_history.append(f"用户: {user_input}")
            self.dialogue_logger.log_entry("用户", user_input)
            
            # 信息收集阶段
            user_input = self._collect_missing_info(user_input, conversation_history)
            
            # 联系商家阶段
            self._contact_merchant(user_input, conversation_history)
            
            # 商家对话循环
            self._merchant_conversation_loop(conversation_history)
            
        except KeyboardInterrupt:
            print("\n程序被用户中断")
        except Exception as e:
            logging.error(f"程序运行出错: {e}")
        finally:
            self._finalize_session()
    
    def _collect_missing_info(self, user_input: str, conversation_history: List[str]) -> str:
        """收集缺失的预订信息"""
        while True:
            info_check = self.chatbot.check_if_more_info_needed(
                user_input, "\n".join(conversation_history)
            )
            
            if "信息完整" in info_check:
                print(f"[INFO] AI信息检查: 信息完整")
                break
            
            print(f"[INFO] AI信息检查: {info_check}")
            ai_question = self.chatbot.generate_ai_question_for_user(
                info_check, "\n".join(conversation_history)
            )
            
            print(f"AI对用户: {ai_question}")
            self.dialogue_logger.log_entry("AI", ai_question)
            
            additional_info = input("用户，请补充信息：\n> ")
            user_input = f"{user_input} {additional_info}"  # 合并信息
            conversation_history.append(f"用户: {additional_info}")
            self.dialogue_logger.log_entry("用户", additional_info)
        
        return user_input
    
    def _contact_merchant(self, user_input: str, conversation_history: List[str]) -> None:
        """联系商家"""
        ai_to_merchant = self.chatbot.generate_ai_message_for_merchant(
            user_input, "\n".join(conversation_history)
        )
        
        print(f"AI对商家: {ai_to_merchant}")
        generate_and_play_voice(ai_to_merchant)
        self.dialogue_logger.log_entry("AI", ai_to_merchant)
        conversation_history.append(f"商家: {ai_to_merchant}")
        self.dialogue_logger.log_entry("商家", ai_to_merchant)
    
    def _merchant_conversation_loop(self, conversation_history: List[str]) -> None:
        """商家对话循环"""
        while True:
            merchant_input = input("商家，请输入你的回复（输入'结束'完成预定）：\n> ")
            
            if merchant_input.strip() == "结束":
                print("预定流程结束。")
                break
            
            conversation_history.append(f"商家: {merchant_input}")
            self.dialogue_logger.log_entry("商家", merchant_input)
            
            reply_type = self.chatbot.classify_merchant_reply(
                merchant_input, "\n".join(conversation_history)
            )
            print(f"[INFO] AI判断商家回复类型: {reply_type}")
            
            if reply_type in ("waiting", "continue"):
                print("[INFO] 商家正在处理中，等待下一步回复...")
                continue
            elif reply_type == "success":
                self._handle_success_reply(merchant_input, conversation_history)
                break
            elif reply_type == "need_user":
                self._handle_need_user_reply(merchant_input, conversation_history)
    
    def _handle_success_reply(self, merchant_input: str, conversation_history: List[str]) -> None:
        """处理预订成功回复"""
        ai_response = self.chatbot.generate_ai_message_for_user_from_merchant(
            merchant_input, "\n".join(conversation_history), is_final=True
        )
        self.dialogue_logger.log_entry("AI", ai_response)
        
        print("\n=== 预定流程完成 ===")
        print(f"最终结果: {ai_response}")
    
    def _handle_need_user_reply(self, merchant_input: str, conversation_history: List[str]) -> None:
        """处理需要用户补充信息的回复"""
        # 识别缺失信息类型
        missing_info_summary = self.chatbot.identify_missing_info_type(
            merchant_input, "\n".join(conversation_history)
        )
        print(f"[INFO] AI识别到商家需要的信息类型摘要: {missing_info_summary}")
        
        # 检查信息是否已提供
        info_status = self.chatbot.check_if_info_already_provided_by_user(
            missing_info_summary, "\n".join(conversation_history)
        )
        print(f"[INFO] AI核对历史信息状态: {info_status}")
        
        if info_status.lower() == "已提供":
            self._relay_existing_info(missing_info_summary, conversation_history)
        else:
            self._request_new_info(merchant_input, conversation_history)
    
    def _relay_existing_info(self, missing_info_summary: str, conversation_history: List[str]) -> None:
        """转发已有信息"""
        actual_info_value = self.chatbot.extract_actual_info_value_from_history(
            missing_info_summary, "\n".join(conversation_history)
        )
        print(f"[INFO] AI从历史提取到具体信息: {actual_info_value}")
        
        conversation_history.append(f"用户: {actual_info_value}")
        self.dialogue_logger.log_entry("用户", actual_info_value)
        
        ai_to_merchant = self.chatbot.generate_ai_message_for_merchant_from_user(
            actual_info_value, "\n".join(conversation_history)
        )
        print(f"AI对商家: {ai_to_merchant}")
        generate_and_play_voice(ai_to_merchant)
        self.dialogue_logger.log_entry("AI", ai_to_merchant)
        conversation_history.append(f"商家: {ai_to_merchant}")
        self.dialogue_logger.log_entry("商家", ai_to_merchant)
    
    def _request_new_info(self, merchant_input: str, conversation_history: List[str]) -> None:
        """请求新信息"""
        ai_response = self.chatbot.generate_ai_message_for_user_from_merchant(
            merchant_input, "\n".join(conversation_history)
        )
        print(f"AI对用户: {ai_response}")
        self.dialogue_logger.log_entry("AI", ai_response)
        
        user_supplement = input("用户，请补充所需信息：\n> ")
        conversation_history.append(f"用户: {user_supplement}")
        self.dialogue_logger.log_entry("用户", user_supplement)
        
        ai_to_merchant = self.chatbot.generate_ai_message_for_merchant_from_user(
            user_supplement, "\n".join(conversation_history)
        )
        print(f"AI对商家: {ai_to_merchant}")
        generate_and_play_voice(ai_to_merchant)
        self.dialogue_logger.log_entry("AI", ai_to_merchant)
        conversation_history.append(f"商家: {ai_to_merchant}")
        self.dialogue_logger.log_entry("商家", ai_to_merchant)
    
    def _finalize_session(self) -> None:
        """完成会话，进行反思和日志管理"""
        try:
            # 获取完整对话日志
            full_log = self.dialogue_logger.read_log()
            
            # 生成AI反思
            ai_reflection = self.chatbot.reflect_on_conversation(full_log)
            print("\n=== AI自我反思与改进建议 ===")
            print(ai_reflection)
            
            # 保存反思
            self.reflection_logger.add_reflection(ai_reflection)
            
            # 精炼反思日志
            all_reflections = self.reflection_logger.get_all_reflections()
            if len(all_reflections) > self.config.max_reflection_entries:
                refined = self.chatbot.refine_reflections(all_reflections)
                self.reflection_logger.refine_reflections(refined)
                
        except Exception as e:
            logging.error(f"会话总结失败: {e}")


def main() -> None:
    """主函数"""
    app = ChatbotApp()
    app.run()


if __name__ == "__main__":
    main()