import json
import uuid
from typing import Any, AsyncGenerator, Dict

import database
import permission
import prompts
import tools
from model_manager import model_manager


class OmniAgentCore:
    def __init__(self):
        pass

    async def run_react_stream(
        self,
        session_id: str,
        user_message: str,
        max_iterations: int = 5
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Executes the ReAct loop and streams out progress events via async generator.
        Events yielded:
        - {"type": "thought", "content": "..."}
        - {"type": "tool_start", "name": "...", "arguments": {...}}
        - {"type": "permission_intercept", "request_id": "...", "reason": "...", "tool_name": "...", "arguments": {...}}
        - {"type": "tool_result", "name": "...", "result": "..."}
        - {"type": "answer_chunk", "content": "..."}
        - {"type": "done", "total_steps": N}
        - {"type": "error", "message": "..."}
        """
        # 1. Build messages context
        system_prompt = prompts.build_system_prompt()
        
        # Load conversation history for this session (sliding window: last 6 messages)
        history_msgs = database.get_session_messages(session_id)
        context_messages = [{"role": "system", "content": system_prompt}]
        
        # Add past history
        for m in history_msgs[-6:]:
            context_messages.append({"role": m["role"], "content": m["content"]})
            
        # Add current user message
        context_messages.append({"role": "user", "content": user_message})
        
        # Save user message to database
        database.save_message(session_id, "user", user_message)

        # Get all tools (native + MCP)
        all_tools = tools.get_all_tool_definitions()

        iteration = 0
        all_thoughts = []
        all_tool_calls_record = []
        final_answer = ""

        client, model_name = model_manager.get_client_and_model()

        while iteration < max_iterations:
            iteration += 1
            
            try:
                # Call LLM with tool schemas
                response = client.chat.completions.create(
                    model=model_name,
                    messages=context_messages,
                    tools=all_tools if all_tools else None,
                    tool_choice="auto" if all_tools else None,
                    temperature=0.2
                )
            except Exception as e:
                yield {"type": "error", "message": f"LLM API ({model_name}) 调用失败: {str(e)}"}
                return

            choice = response.choices[0]
            msg = choice.message
            
            # Check for native reasoning_content (DeepSeek-V4-Pro / R1) and text content
            reasoning_text = getattr(msg, "reasoning_content", None) or ""
            content_text = msg.content or ""
            thought_text = reasoning_text if reasoning_text else content_text
            
            # If the model produced tool calls
            if msg.tool_calls:
                # If there was reasoning or introductory thought text, yield it
                if thought_text:
                    all_thoughts.append(thought_text)
                    yield {"type": "thought", "content": thought_text}
                
                # Append assistant message with tool calls to context
                context_messages.append(msg.model_dump())
                
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except Exception:
                        fn_args = {"raw": tc.function.arguments}
                    
                    yield {
                        "type": "tool_start",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "arguments": fn_args
                    }
                    
                    # --- Enterprise Permission Guard Check ---
                    risk, reason, req_id = permission.check_permission(fn_name, fn_args, session_id)
                    if risk == permission.PermissionRisk.ASK_USER:
                        yield {
                            "type": "permission_intercept",
                            "request_id": req_id,
                            "tool_name": fn_name,
                            "arguments": fn_args,
                            "reason": reason
                        }
                        tool_output = f"[安全拦截] 此高危操作已被安全网关暂停，等待用户人工审批确认 (请求ID: {req_id})。"
                    elif risk == permission.PermissionRisk.DENY:
                        tool_output = f"[严重越权拒绝] 系统已阻断该操作：{reason}"
                    else:
                        # Allowed -> Execute tool!
                        try:
                            if fn_name == "spawn_subagent":
                                sub_role = fn_args.get("role", "专属专家子智能体")
                                sub_task = fn_args.get("task", "")
                                yield {
                                    "type": "subagent_start",
                                    "role": sub_role,
                                    "task": sub_task,
                                    "tool_call_id": tc.id
                                }
                                tool_output = await self.run_subagent_task(session_id, sub_role, sub_task)
                                yield {
                                    "type": "subagent_done",
                                    "role": sub_role,
                                    "result": tool_output,
                                    "tool_call_id": tc.id
                                }
                            else:
                                tool_output = tools.dispatch_tool(fn_name, fn_args)
                        except Exception as e:
                            tool_output = f"工具执行发生异常: {str(e)}"
                            
                    all_tool_calls_record.append({
                        "id": tc.id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "output": tool_output[:1000]
                    })

                    yield {
                        "type": "tool_result",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "result": tool_output
                    }

                    # Feed tool output back to conversation context
                    context_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(tool_output)
                    })
                    
                # Continue next iteration of ReAct loop
                continue

            else:
                # No tool calls -> Model has formed final answer
                if reasoning_text:
                    all_thoughts.append(reasoning_text)
                    yield {"type": "thought", "content": reasoning_text}

                final_answer = content_text
                
                # Stream the final answer in small chunks to provide pleasant typing effect
                chunk_size = 12
                for i in range(0, len(final_answer), chunk_size):
                    yield {"type": "answer_chunk", "content": final_answer[i:i+chunk_size]}
                
                # Save assistant response to DB
                database.save_message(
                    session_id=session_id,
                    role="assistant",
                    content=final_answer,
                    thought="\n\n".join(all_thoughts) if all_thoughts else None,
                    tool_calls=all_tool_calls_record if all_tool_calls_record else None
                )

                yield {"type": "done", "total_steps": iteration}
                return

        # If loop reached max iterations without final answer
        fallback_msg = "智能体多轮推理已达上限 (5轮)，已为您保存当前所有执行过程与中间文件。"
        yield {"type": "answer_chunk", "content": fallback_msg}
        yield {"type": "done", "total_steps": max_iterations}

    async def run_subagent_task(self, session_id: str, role: str, task: str) -> str:
        """
        DeerFlow-style SubAgent Execution:
        Spawns an isolated subagent with specialized role and tools to complete a focused subtask.
        """
        sub_id = "sub_" + str(uuid.uuid4())[:8]
        database.record_subagent_task(sub_id, session_id, role, task)

        sub_system = f"""你是专职于【{role}】的专属子智能体 (Sub-Agent)。
受主智能体指派，你负责高效、专注地完成以下独立子任务：
【子任务目标】：{task}

工作区位于 ./workspace。
你可以使用基础工具（file_write, file_read, file_list, bash_run, python_eval, web_fetch, rag_search）来执行调查、编写代码或数据分析。
【行动要求】：
1. 专注完成本子任务，不要发散。
2. 如需写代码或生成文件，请直接在 ./workspace 中产出。
3. 任务完成后，提供精炼、清晰的结构化总结汇报。
"""
        # Sub-agents do not recursively spawn subagents
        subagent_tools = [t for t in tools.get_all_tool_definitions() if t["function"]["name"] != "spawn_subagent"]

        sub_messages = [
            {"role": "system", "content": sub_system},
            {"role": "user", "content": f"请开始执行子任务: {task}"}
        ]

        sub_iter = 0
        final_report = ""
        client, model_name = model_manager.get_client_and_model()

        while sub_iter < 4:
            sub_iter += 1
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=sub_messages,
                    tools=subagent_tools if subagent_tools else None,
                    tool_choice="auto" if subagent_tools else None,
                    temperature=0.2
                )
            except Exception as e:
                final_report = f"子智能体 ({model_name}) 调用失败: {str(e)}"
                break

            sub_choice = resp.choices[0].message
            if sub_choice.tool_calls:
                sub_messages.append(sub_choice.model_dump())
                for stc in sub_choice.tool_calls:
                    s_name = stc.function.name
                    try:
                        s_args = json.loads(stc.function.arguments)
                    except Exception:
                        s_args = {}
                    
                    # Direct dispatch in workspace
                    try:
                        s_out = tools.dispatch_tool(s_name, s_args)
                    except Exception as err:
                        s_out = f"工具执行异常: {str(err)}"

                    sub_messages.append({
                        "role": "tool",
                        "tool_call_id": stc.id,
                        "content": str(s_out)
                    })
            else:
                final_report = sub_choice.content or "子任务已执行完成。"
                break

        if not final_report:
            final_report = f"子智能体【{role}】已执行相关操作并产出中间结果。"

        database.update_subagent_task(sub_id, "COMPLETED", final_report)
        return f"【子智能体 ({role}) 汇报】\n{final_report}"

agent_core = OmniAgentCore()
