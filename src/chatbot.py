import re
from openai import OpenAI
from . import api
import datetime
import sensenova

sensenova.access_key_id = api.SENSENOVA_ACCESS_KEY_ID
sensenova.secret_access_key = api.SENSENOVA_SECRET_ACCESS_KEY

# 设置你的OpenAI API Key
client = OpenAI(api_key=api.OPENAI_API_KEY,
                base_url = api.OPENAI_BASE_URL)


def ask_gpt(messages):
    response = client.chat.completions.create(
        model=api.OPENAI_MODEL,
        messages=messages
    )
    return response.choices[0].message.content

def check_if_more_info_needed(user_input, conversation_history):
    messages = [
        {"role": "system", "content": "你是一个餐厅预定助手。请分析用户输入和对话历史，判断是否还需要更多信息才能完成预定。如果需要更多信息，请明确指出缺少什么信息；如果信息足够，请严格回复四个字'信息完整'。特别注意：联系方式是完成预定的必要信息，无论如何都要确保已收集到联系方式。"},
        {"role": "user", "content": f"用户输入: {user_input}\n对话历史: {conversation_history}\n请判断是否需要更多信息。"}
    ]
    response = ask_gpt(messages)
    return response

def get_latest_reflection(log_path="logs/chatbot_reflection_log.txt"):
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        idxs = [i for i, line in enumerate(lines) if line.startswith("====")]
        if not idxs:
            return ""
        last_idx = idxs[-1]
        # 取到下一个====或文件结尾
        next_idx = next((i for i in idxs if i > last_idx), len(lines))
        reflection = "".join(lines[last_idx+1:next_idx]).strip()
        return reflection
    except Exception:
        return ""

def generate_ai_question_for_user(missing_info, conversation_history):
    reflection = get_latest_reflection()
    prompt = f"你是餐厅预定助手。请根据以下缺失信息，友好地向用户追问：{missing_info}。不要重复询问已经问过的问题，请根据对话历史判断哪些信息已提供。严禁使用表情符号、拟人化、情绪化、客套语或任何不专业的表达。"
    if reflection:
        prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"对话历史: {conversation_history}"}
    ]
    return ask_gpt(messages)

def generate_ai_message_for_merchant(user_input, conversation_history):
    reflection = get_latest_reflection()
    prompt = (
        "你是餐厅预定助手。请以用户的身份，用最简洁、直接、事实、专业的口吻，向商家转述用户的完整预定需求。"
        "只传递用户核心意图和关键信息，严禁任何闲聊、主观感受、拟人化表达、多余修饰或表情符号。"
        "使用第一人称'我'或'我们'进行表达，例如：'我需要预定...'，'我们有5人...'。"
    )
    if reflection:
        prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"用户输入: {user_input}\n对话历史: {conversation_history}"}
    ]
    return ask_gpt(messages)

def generate_ai_message_for_user_from_merchant(merchant_input, conversation_history, is_final=False):
    reflection = get_latest_reflection()
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

def generate_ai_message_for_merchant_from_user(user_input, conversation_history):
    reflection = get_latest_reflection()
    prompt = (
        "你是餐厅预定助手。请以用户的身份，用最简洁、直接、事实、专业的口吻，将用户的最新决策或补充信息转述给商家。"
        "只传递用户核心意图和关键信息，严禁任何闲聊、主观感受、拟人化表达、多余修饰或表情符号。"
        '例如："我同意七点的座位，请帮忙确认预定。"或"我的联系电话是138xxxx，请用此号确认预定。"'
        "使用第一人称'我'或'我们'进行表达。"
    )
    if reflection:
        prompt = f"【请注意以下自我反思与改进建议：{reflection}】\n" + prompt
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"用户最新输入: {user_input}\n对话历史: {conversation_history}"}
    ]
    return ask_gpt(messages)

def classify_merchant_reply(merchant_input, conversation_history):
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

def summarize_conversation(conversation_history):
    prompt = (
        "你是一个对话总结助手。请根据以下用户和商家的历史对话，提炼出本次预定的关键信息（如时间、人数、餐厅、联系方式、特殊需求等），用简洁条理的方式总结。"
        "对话历史如下：\n" + conversation_history
    )
    messages = [
        {"role": "system", "content": prompt}
    ]
    return ask_gpt(messages)

def reflect_on_conversation(conversation_history):
    prompt = (
        "你是一个AI对话反思助手。请根据以下用户和商家的历史对话，反思本次预定流程中AI的表现，指出存在的问题或可以改进的地方，并给出具体改进建议。"
        "对话历史如下：\n" + conversation_history
    )
    messages = [
        {"role": "system", "content": prompt}
    ]
    return ask_gpt(messages)

def refine_reflection_log(log_path="logs/chatbot_reflection_log.txt", max_entries=5):
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        idxs = [i for i, line in enumerate(lines) if line.startswith("====")]
        if len(idxs) <= max_entries:
            return  # 不需要精炼
        # 提取所有反思内容
        all_reflections = []
        for i in range(len(idxs)):
            start = idxs[i] + 1
            end = idxs[i+1] if i+1 < len(idxs) else len(lines)
            all_reflections.append("".join(lines[start:end]).strip())
        # 用AI精炼
        prompt = (
            "你是AI反思总结助手。请将以下多条AI自我反思与改进建议进行归纳、去重、精炼，合并为一条最有用、最具指导性的反思建议，便于后续prompt改进：\n"
            + "\n\n".join(all_reflections)
        )
        messages = [{"role": "system", "content": prompt}]
        summary = ask_gpt(messages)
        # 覆盖原文件，只保留一条精炼后的反思
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"\n==== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ==== (精炼)\n")
            f.write(summary.strip() + "\n")
    except Exception as e:
        print(f"[WARNING] 反思日志精炼失败: {e}")

def log_dialogue_entry(speaker, content, log_path="logs/ai_generated_log.txt"):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"==== {timestamp} ====\n")
        f.write(f"[{speaker}]: {content.strip()}\n")

def reflect_on_ai_generated(log_path="logs/ai_generated_log.txt"):
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            full_log_content = f.read()
        
        # 不再过滤，直接将完整日志内容传递给AI，但Prompt会指导AI只反思自己的表现
        ai_lines_for_reflection = full_log_content
        
    except Exception:
        ai_lines_for_reflection = ""
    
    prompt = (
        "你是一个AI自我反思助手。请根据以下完整的对话日志，"\
        "深入反思AI在整个对话流程中的表现，指出存在的问题或可以改进的地方，并给出具体改进建议。"\
        "请务必将反思的焦点限定在AI自身（你）的言行和决策上，不要去评价用户或商家的行为。"\
        "特别是要考虑AI是否能更有效地引导对话、提供信息、或处理特殊情况。"\
        "完整对话日志如下：\n" + ai_lines_for_reflection
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请根据以上对话日志反思AI的表现。"}
    ]
    return ask_gpt(messages)

def clear_ai_generated_log(log_path="logs/ai_generated_log.txt"):
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.truncate(0)  # 清空文件内容
    except Exception as e:
        print(f"[WARNING] 清空AI生成日志失败: {e}")

def identify_missing_info_type(merchant_input, conversation_history):
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

def check_if_info_already_provided_by_user(info_type_summary, conversation_history):
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

def extract_actual_info_value_from_history(info_type_summary, conversation_history):
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

def main():
    clear_ai_generated_log() # 在每次新会话开始时清空AI生成日志
    print("=== 语音预定订餐系统 Demo ===")
    conversation_history = []
    user_input = input("用户，请输入你的预定需求：\n> ")
    conversation_history.append(f"用户: {user_input}")
    log_dialogue_entry("用户", user_input)
    # 只在联系商家前做信息检查
    while True:
        info_check = check_if_more_info_needed(user_input, "\n".join(conversation_history))
        if "信息完整" not in info_check:
            print(f"[INFO] AI信息检查: {info_check}")
            ai_question = generate_ai_question_for_user(info_check, "\n".join(conversation_history))
            print(f"AI对用户: {ai_question}")
            log_dialogue_entry("AI", ai_question)
            additional_info = input("用户，请补充信息：\n> ")
            user_input = f"{user_input} {additional_info}"  # 合并信息
            conversation_history.append(f"用户: {additional_info}")
            log_dialogue_entry("用户", additional_info)
            continue
        else:
            print(f"[INFO] AI信息检查: 信息完整")
            break
    # 联系商家
    ai_to_merchant = generate_ai_message_for_merchant(user_input, "\n".join(conversation_history))
    print(f"AI对商家: {ai_to_merchant}")
    log_dialogue_entry("AI", ai_to_merchant)
    conversation_history.append(f"商家: {ai_to_merchant}")
    log_dialogue_entry("商家", ai_to_merchant)
    # 商家回复循环
    while True:
        merchant_input = input("商家，请输入你的回复（输入'结束'完成预定）：\n> ")
        if merchant_input.strip() == "结束":
            print("预定流程结束。")
            ai_reflection = reflect_on_ai_generated()
            print("\n=== AI自我反思与改进建议 ===")
            print(ai_reflection)
            with open("logs/chatbot_reflection_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n==== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====\n")
                f.write(ai_reflection + "\n")
            refine_reflection_log()
            return
        conversation_history.append(f"商家: {merchant_input}")
        log_dialogue_entry("商家", merchant_input)
        reply_type = classify_merchant_reply(merchant_input, "\n".join(conversation_history))
        print(f"[INFO] AI判断商家回复类型: {reply_type}")
        if reply_type in ("waiting", "continue"):
            print("[INFO] 商家正在处理中，等待下一步回复...")
            continue
        elif reply_type == "success":
            ai_response = generate_ai_message_for_user_from_merchant(merchant_input, "\n".join(conversation_history), is_final=True)
            log_dialogue_entry("AI", ai_response)
            print("\n=== 预定流程完成 ===")
            print(f"最终结果: {ai_response}") # 确保只打印一次最终结果
            ai_reflection = reflect_on_ai_generated()
            print("\n=== AI自我反思与改进建议 ===")
            print(ai_reflection)
            with open("logs/chatbot_reflection_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n==== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====\n")
                f.write(ai_reflection + "\n")
            refine_reflection_log()
            return
        elif reply_type == "need_user":
            # Step 1: AI识别商家需要的信息类型
            missing_info_summary = identify_missing_info_type(merchant_input, "\n".join(conversation_history))
            print(f"[INFO] AI识别到商家需要的信息类型摘要: {missing_info_summary}")

            # Step 2: AI检查该信息类型是否已在历史中提供
            info_status = check_if_info_already_provided_by_user(missing_info_summary, "\n".join(conversation_history))
            print(f"[INFO] AI核对历史信息状态: {info_status}")

            if info_status.lower() == "已提供":
                # 信息已在历史中，提取并转述给商家
                actual_info_value = extract_actual_info_value_from_history(missing_info_summary, "\n".join(conversation_history))
                print(f"[INFO] AI从历史提取到具体信息: {actual_info_value}")

                user_response_for_merchant_relay = actual_info_value  # 模拟用户提供
                conversation_history.append(f"用户: {user_response_for_merchant_relay}")
                log_dialogue_entry("用户", user_response_for_merchant_relay)
                ai_to_merchant_from_user_supplement = generate_ai_message_for_merchant_from_user(user_response_for_merchant_relay, "\n".join(conversation_history))
                print(f"AI对商家: {ai_to_merchant_from_user_supplement}")
                log_dialogue_entry("AI", ai_to_merchant_from_user_supplement)
                conversation_history.append(f"商家: {ai_to_merchant_from_user_supplement}")
                log_dialogue_entry("商家", ai_to_merchant_from_user_supplement)
                continue  # 回到商家回复循环
            else:
                # 信息未在历史中，向用户提问
                ai_response_to_user_from_merchant = generate_ai_message_for_user_from_merchant(merchant_input, "\n".join(conversation_history))
                print(f"AI对用户: {ai_response_to_user_from_merchant}")
                log_dialogue_entry("AI", ai_response_to_user_from_merchant)
                user_input_supplement = input("用户，请补充所需信息：\n> ")
                conversation_history.append(f"用户: {user_input_supplement}")
                log_dialogue_entry("用户", user_input_supplement)
                ai_to_merchant_from_user_supplement = generate_ai_message_for_merchant_from_user(user_input_supplement, "\n".join(conversation_history))
                print(f"AI对商家: {ai_to_merchant_from_user_supplement}")
                log_dialogue_entry("AI", ai_to_merchant_from_user_supplement)
                conversation_history.append(f"商家: {ai_to_merchant_from_user_supplement}")
                log_dialogue_entry("商家", ai_to_merchant_from_user_supplement)
                continue  # 回到商家回复循环

if __name__ == "__main__":
    main()