from typing import Optional
from urllib.parse import urlparse
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.log import get_logger
import requests
from unidiff import PatchSet
from requests.auth import HTTPBasicAuth

TEMP_REVIEW_COMMIT_ID = "PR-Agent-Temp"
FINAL_REVIEW_COMMIT_ID = "PR-Agent-Final"


class GiteaProvider(GitProvider):
    def __init__(self, pr_url: str):
        self.pr_url = pr_url
        self.api_url, self.owner, self.repo, self.pr_id = self._parse_pr_url(pr_url)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.session.headers.update({"User-Agent": "Gitea-Python-Client/1.0.0"})
        self.session.headers.update({"Content-Type": "application/json"})
        # 如果需要身份验证，请在此添加，例如：
        self.session.headers.update(
            {
                "Authorization": f'Bearer {get_settings().get("GITEA.BEARER_TOKEN", "367464d8aa7e1dceba1413ada674a75b4e3e3d45")}'
            }
        )

        self.headers = self.session.headers
        self.pr = None
        self.temp_comments = []
        self.files = []
        if pr_url:
            self.set_pr()

    def _parse_pr_url(self, pr_url: str):
        parsed_url = urlparse(pr_url)
        path_parts = parsed_url.path.strip("/").split("/")
        if len(path_parts) < 4 or path_parts[-2] != "pulls":
            raise ValueError(f"Invalid Gitea PR URL: {pr_url}")
        owner, repo, _, pr_id = (
            path_parts[-4],
            path_parts[-3],
            path_parts[-2],
            path_parts[-1],
        )
        api_url = f"{parsed_url.scheme}://{parsed_url.netloc}/api/v1"
        return api_url, owner, repo, pr_id

    def _request(self, method: str, endpoint: str, **kwargs):
        url = f"{self.api_url}/repos/{self.owner}/{self.repo}/{endpoint}"
        response = requests.request(
            method,
            url,
            headers=self.headers,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def set_pr(self):
        self.pr = self._get_pr()

    def _get_pr(self):
        try:
            pr = self._request("GET", f"pulls/{self.pr_id}")
            return type("new_dict", (object,), pr)
        except Exception as e:
            get_logger().error(f"Failed to get pull request, error: {e}")
            raise e

    def is_supported(self, capability: str) -> bool:
        # 实现检查 Gitea 是否支持特定功能的逻辑
        if capability in [
            "get_issue_comments",
            "create_inline_comment",
            "publish_inline_comments",
            "publish_file_comments",
            "gfm_markdown",
        ]:  # gfm_markdown is supported in gitlab !
            return False
        return True

    def get_files(self) -> list:
        # 实现获取 PR 中所有文件的逻辑
        return [
            file["filename"]
            for file in self._request("GET", f"pulls/{self.pr_id}/files")
        ]

    def get_diff_files(self) -> list:
        # 实现获取 PR 中差异文件的逻辑
        # 因为需要返回text，所以这里直接使用 requests
        url = f"{self.api_url}/repos/{self.owner}/{self.repo}/pulls/{self.pr_id}.patch"
        response = requests.request("GET", url, headers=self.headers)
        response.raise_for_status()
        patch = response.text
        patch_set = PatchSet(patch.splitlines())  # 解析 diff

        file_patches = []
        files = self._request("GET", f"pulls/{self.pr_id}/files")
        self.files = files
        for file in files:
            filename = file["filename"]
            # 统计新增和删除的行数
            num_plus = file["additions"]
            num_minus = file["deletions"]
            # 计算编辑类型
            if num_plus > 0 and num_minus > 0:
                edit_type = EDIT_TYPE.MODIFIED
            elif num_plus > 0:
                edit_type = EDIT_TYPE.ADDED
            elif num_minus > 0:
                edit_type = EDIT_TYPE.DELETED
            else:
                edit_type = EDIT_TYPE.UNKNOWN

            head_file_raw_url = file["raw_url"]
            response = requests.request("GET", head_file_raw_url, headers=self.headers)
            response.raise_for_status()
            head_file_name = file["filename"]
            head_file = response.text
            base_file_name = None
            base_file = ""
            # 将字符串转换成按行的列表
            head_file_lines = head_file.splitlines(keepends=True)
            # 遍历 patch，反向应用修改
            for patched_file in patch_set:
                if patched_file.path == filename:
                    base_file_lines = head_file_lines[:]  # 复制新文件内容
                    # 逆序应用 hunk
                    for hunk in reversed(patched_file):
                        for line in reversed(hunk):
                            if line.is_added:  # 删除新增的行
                                base_file_lines.pop(line.target_line_no - 1)
                            elif line.is_removed:  # 重新插入被删除的行
                                base_file_lines.insert(
                                    line.source_line_no - 1, line.value
                                )
                    base_file = "".join(base_file_lines)
                    base_file_name = patched_file.source_file

            file_patches.append(
                FilePatchInfo(
                    base_file=base_file,
                    head_file=head_file,
                    patch=patch,
                    filename=filename,
                    old_filename=base_file_name,
                    num_plus_lines=num_plus,
                    num_minus_lines=num_minus,
                    edit_type=edit_type,
                )
            )

        self.diff_files = file_patches
        return file_patches

    def publish_description(self, pr_title: str, pr_body: str):
        # 实现发布 PR 描述的逻辑
        self._request(
            "PATCH", f"pulls/{self.pr_id}", json={"title": pr_title, "body": pr_body}
        )

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        inline_comments = []
        # 实现发布代码建议的逻辑
        try:
            for suggestion in code_suggestions:
                body = suggestion["body"]
                relevant_file = suggestion["relevant_file"]
                relevant_lines_start = suggestion["relevant_lines_start"]

                diff_files = self.get_diff_files()
                target_file = None
                for file in diff_files:
                    if file.filename == relevant_file:
                        if file.filename == relevant_file:
                            target_file = file
                            break
                body = body.replace(
                    "```suggestion", f"```{target_file.filename.replace("\n", "")}"
                )
                target_line_no = relevant_lines_start + 1
                inline_comments.append(
                    {
                        "body": body,
                        "new_position": target_line_no,
                        "path": target_file.filename.replace("\n", ""),
                    }
                )

            self._request(
                "POST",
                f"pulls/{self.pr_id}/reviews",
                json={
                    "body": "## PR Code Suggestions ✨",
                    "comments": inline_comments,
                    "event": "REQUEST_REVIEW",
                },
            )
            get_logger().info(f"Comment posted")
        except Exception as e:
            get_logger().exception(
                f"Could not publish code suggestion:\nsuggestion: {suggestion}\nerror: {e}"
            )

        # note that we publish suggestions one-by-one. so, if one fails, the rest will still be published
        return True

    def send_inline_comment(
        self, body: str, file_path: str, line_number: int, is_new_line: bool = True
    ):
        payload = {
            "body": body,
            "file_path": file_path,
            "line": line_number,
            "side": "RIGHT" if is_new_line else "LEFT",
        }
        try:
            self._request(
                "POST",
                f"pulls/{self.pr_id}/reviews",
                json={
                    "body": body,
                    "commit_id": FINAL_REVIEW_COMMIT_ID,
                },
            )
            get_logger().info(f"Comment posted to {file_path}:{line_number}")
        except Exception as e:
            get_logger().error(f"Failed to post comment: {e}")

    def get_languages(self):
        # 实现获取仓库使用语言的逻辑
        return self._request("GET", "languages")

    def get_pr_branch(self):
        # 实现获取 PR 分支信息的逻辑
        pr_info = self._request("GET", f"pulls/{self.pr_id}")
        return pr_info["head"]["ref"]

    def get_user_id(self):
        # 实现获取用户 ID 的逻辑
        user_info = self._request("GET", "user")
        return user_info["id"]

    def get_pr_description_full(self) -> str:
        # 实现获取完整的 PR 描述的逻辑
        pr_info = self._request("GET", f"pulls/{self.pr_id}")
        return pr_info.get("body", "")

    def get_repo_settings(self):
        # 实现获取仓库设置的逻辑
        pass

    def get_line_link(
        self,
        relevant_file: str,
        relevant_line_start: int,
        relevant_line_end: int = None,
    ) -> str:
        file_html_url = ""
        for file in self.files:
            if file["filename"] == relevant_file:
                file_html_url = file["html_url"]
                break
        if relevant_line_start == -1:
            link = f"{file_html_url}"
        elif relevant_line_end:
            link = f"{file_html_url}#L{relevant_line_start}-L{relevant_line_end}"
        else:
            link = f"{file_html_url}#L{relevant_line_start}"
        return link

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not is_temporary:
            self._request(
                "POST",
                f"pulls/{self.pr_id}/reviews",
                json={
                    "body": pr_comment,
                    "commit_id": FINAL_REVIEW_COMMIT_ID,
                },
            )

    def publish_persistent_comment(
        self,
        pr_comment: str,
        initial_header: str,
        update_header: bool = True,
        name="review",
        final_update_message=True,
    ):
        # 实现发布持久评论的逻辑
        self.publish_comment(pr_comment, is_temporary=False)
        # pass

    def publish_inline_comment(
        self,
        body: str,
        relevant_file: str,
        relevant_line_in_file: str,
        original_suggestion=None,
    ):
        print(
            f"发布行内评论，文件: {relevant_file}, 行号: {relevant_line_in_file}, 评论内容: {body}"
        )
        raise NotImplementedError(
            "Gitea provider does not support creating inline comments yet"
        )

    def publish_inline_comments(self, comments: list[dict]):
        print(f"发布行内评论，文件: {comments}")
        raise NotImplementedError(
            "Gitea provider does not support publishing inline comments yet"
        )

    def remove_initial_comment(self):
        # 实现删除初始评论的逻辑
        try:
            reviews = self._request(
                "GET",
                f"pulls/{self.pr_id}/reviews",
            )

            for review in reviews:
                if review["commit_id"] == TEMP_REVIEW_COMMIT_ID:
                    print(f"先删除初始评论review['id']: {review['id']}")
                    url = f"{self.api_url}/repos/{self.owner}/{self.repo}/pulls/{self.pr_id}/reviews/{review['id']}"
                    response = requests.request("DELETE", url, headers=self.headers)
                    response.raise_for_status()

        except Exception as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        # 实现获取问题评论的逻辑
        # response = self._request("GET", f"issues/{self.pr_id}/reviews")
        return []

    def publish_labels(self, labels):
        # 实现发布标签的逻辑
        # try:
        #     self.pr.labels = list(set(labels))
        #     self._request("PATCH", f"pulls/{self.pr_id}")
        # except Exception as e:
        #     get_logger().warning(f"Failed to publish labels, error: {e}")
        pass

    def get_pr_labels(self, update=False):
        # 实现获取 PR 标签的逻辑
        # return self.pr.labels
        pass

    def add_eyes_reaction(
        self, issue_comment_id: int, disable_eyes: bool = False
    ) -> Optional[int]:
        # 实现添加“眼睛”反应的逻辑
        return True

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        # 实现移除反应的逻辑
        return True

    def get_commit_messages(self):
        # 实现获取提交消息的逻辑
        return ""
