"""
Harbor 镜像仓库相关的 API 路由
将本地镜像文件推送到本机 Harbor 仓库
"""
import logging
import os
import shutil
import subprocess

from fastapi import APIRouter, HTTPException, Query, UploadFile, File

from api.utils.harbor_util import (
    LOG_PUSH,
    LOG_PUSH_CHART,
    get_download_dir,
    get_repository_ip,
    get_harbor_connection_info,
    run_command,
    parse_chart_info,
    build_display_url,
    build_oci_registry,
    safe_error_handler,
)

router = APIRouter()


@router.post("/upload_chunk")
async def upload_chunk(
    chunk: UploadFile = File(..., description="分片文件"),
    chunk_index: int = Query(..., description="当前分片序号（从0开始）"),
    file_name: str = Query(..., description="文件名称，如 myapp.tar")
):
    """
    分片上传镜像文件到临时目录

    流程：
    1. 读取 harbor.download 配置，构建临时目录 {download}/temporary/{file_name}/
    2. 检查分片序号是否已上传过，若 chunk_index <= 已保存的分片序号则跳过
    3. 将分片文件写入临时目录
    4. 更新分片序号记录
    """
    try:
        download_dir = get_download_dir()
    except HTTPException:
        return {"code": 500, "message": "Harbor 下载目录未配置"}

    # 临时目录: {download}/temporary/{file_name}/
    temp_dir = os.path.join(download_dir, "temporary", file_name)
    os.makedirs(temp_dir, exist_ok=True)

    # 分片序号记录文件
    chunk_record_file = os.path.join(temp_dir, ".chunk_record")

    # 读取已保存的最大分片序号
    saved_chunk_index = -1
    if os.path.exists(chunk_record_file):
        try:
            with open(chunk_record_file, "r", encoding="utf-8") as f:
                saved_chunk_index = int(f.read().strip())
        except Exception:
            saved_chunk_index = -1

    # 如果当前分片序号 <= 已保存的分片序号，跳过
    if chunk_index <= saved_chunk_index:
        return {
            "code": 200,
            "message": f"分片 {chunk_index} 已上传，跳过",
            "max_chunk_index": saved_chunk_index,
        }

    # 写入分片文件
    chunk_file_path = os.path.join(temp_dir, str(chunk_index))
    try:
        content = await chunk.read()
        with open(chunk_file_path, "wb") as f:
            f.write(content)
    except Exception as e:
        logging.error("[harbor_upload_chunk] 写入分片文件失败: %s", e)
        return {"code": 500, "message": f"写入分片文件失败: {str(e)}"}

    # 更新分片序号记录
    try:
        with open(chunk_record_file, "w", encoding="utf-8") as f:
            f.write(str(chunk_index))
    except Exception as e:
        logging.error("[harbor_upload_chunk] 更新分片记录失败: %s", e)
        return {"code": 500, "message": f"更新分片记录失败: {str(e)}"}

    logging.info("[harbor_upload_chunk] 分片 %d 上传成功, 文件: %s",
                 chunk_index, file_name)

    return {
        "code": 200,
        "message": f"分片 {chunk_index} 上传成功",
        "max_chunk_index": chunk_index,
    }


@router.get("/merge_chunks")
async def merge_chunks(
    file_name: str = Query(..., description="文件名称，如 myapp.tar")
):
    """
    合并分片文件为完整文件

    流程：
    1. 在 {download}/temporary/{file_name}/ 下按序号读取所有分片
    2. 按 chunk_index 升序合并写入 {download}/{file_name}
    3. 删除临时文件夹 {download}/temporary/{file_name}/
    """
    download_dir = get_download_dir()

    temp_dir = os.path.join(download_dir, "temporary", file_name)
    if not os.path.isdir(temp_dir):
        raise HTTPException(
            status_code=404,
            detail=f"临时目录不存在: {temp_dir}"
        )

    # 收集所有分片文件（排除隐藏文件 .chunk_record）
    chunk_files = []
    for name in os.listdir(temp_dir):
        if name.startswith("."):
            continue
        try:
            chunk_files.append(int(name))
        except ValueError:
            continue

    if not chunk_files:
        raise HTTPException(
            status_code=400,
            detail=f"临时目录为空，没有分片文件: {temp_dir}"
        )

    # 按序号升序排列
    chunk_files.sort()
    logging.info("[harbor_merge] 共 %d 个分片, 范围: %d ~ %d",
                 len(chunk_files), chunk_files[0], chunk_files[-1])

    # 合并写入目标文件
    target_path = os.path.join(download_dir, file_name)
    total_size = 0
    try:
        with open(target_path, "wb") as outfile:
            for idx in chunk_files:
                chunk_path = os.path.join(temp_dir, str(idx))
                with open(chunk_path, "rb") as infile:
                    data = infile.read()
                    outfile.write(data)
                    total_size += len(data)
    except Exception as e:
        logging.error("[harbor_merge] 合并文件失败: %s", e)
        # 清理可能写入了一半的目标文件
        if os.path.exists(target_path):
            os.remove(target_path)
        raise HTTPException(status_code=500, detail=f"合并文件失败: {str(e)}")

    # 删除临时文件夹
    try:
        shutil.rmtree(temp_dir)
        logging.info("[harbor_merge] 已删除临时目录: %s", temp_dir)
    except Exception as e:
        logging.warning("[harbor_merge] 删除临时目录失败: %s", e)

    logging.info("[harbor_merge] 合并完成: %s, 大小: %d 字节, 分片数: %d",
                 target_path, total_size, len(chunk_files))

    return {
        "code": 200,
        "message": f"分片合并成功: {file_name}",
        "data": {
            "file_name": file_name,
            "target_path": target_path,
            "total_size": total_size,
            "chunk_count": len(chunk_files),
            "chunks": chunk_files,
        }
    }


@router.post("/push")
async def push_image_to_harbor(
    image_file: str = Query(..., description="镜像文件名，如 myapp.tar")
):
    """
    将本地镜像文件推送到本机 Harbor 仓库

    流程：
    1. 通过 utils.util.get_ip() 获取本机 IP 作为 Harbor 仓库地址
    2. 在 config.yaml 的 harbor.download 目录下查找镜像文件
    3. 读取 harbor 配置（agreement、port、username、password、project）
    4. docker load 加载镜像
    5. docker tag 打标签
    6. docker push 推送到本机 Harbor
    7. docker rmi 清理本地镜像
    8. 删除临时文件
    """
    download_dir = get_download_dir()
    repository = get_repository_ip()

    # 在 download 目录下查找镜像文件
    image_path = os.path.join(download_dir, image_file)
    if not os.path.exists(image_path):
        raise HTTPException(
            status_code=404,
            detail=f"镜像文件不存在: {image_path}"
        )

    # 读取 Harbor 连接信息
    conn = get_harbor_connection_info(require_auth=True, project_key="project")
    agreement = conn["agreement"]
    username = conn["username"]
    password = conn["password"]
    port_str = conn["port_str"]
    project = conn["project"]
    harbor_addr_local = conn["harbor_addr_local"]

    harbor_url = f"{agreement}{harbor_addr_local}"
    harbor_addr_remote = f"{repository}{':' + port_str if port_str else ''}"

    steps = []
    file_base, _ = os.path.splitext(image_file)

    try:
        # Step 1: docker load 加载镜像
        logging.info("[%s] 加载镜像: %s", LOG_PUSH, image_path)
        load_cmd = ["docker", "load", "-i", image_path]
        load_stdout, load_stderr = run_command(
            load_cmd, timeout=300, log_prefix=LOG_PUSH,
            step_name="load", step_desc="docker load", steps=steps,
        )

        # 从输出中提取镜像名，如 "Loaded image: xxx:latest"
        loaded_image = None
        for line in load_stdout.split("\n"):
            line = line.strip()
            if line.startswith("Loaded image:") or line.startswith("Loaded image ID:"):
                loaded_image = line.split(":", 1)[1].strip()
                break
        if not loaded_image:
            loaded_image = file_base

        logging.info("[%s] 加载成功: %s", LOG_PUSH, loaded_image)

        # Step 2: docker login
        logging.info("[%s] 登录 Harbor: %s", LOG_PUSH, harbor_url)
        login_cmd = [
            "docker", "login", harbor_url,
            "-u", username,
            "--password-stdin",
        ]
        run_command(
            login_cmd, timeout=30, input_str=password,
            log_prefix=LOG_PUSH, step_name="login",
            step_desc="docker login", steps=steps,
        )

        # Step 3: docker tag
        image_name = loaded_image.split("/")[-1]
        target_image = f"{harbor_addr_local}/{project}/{image_name}"
        logging.info("[%s] 打标签: %s -> %s", LOG_PUSH, loaded_image, target_image)
        run_command(
            ["docker", "tag", loaded_image, target_image],
            timeout=30, log_prefix=LOG_PUSH, step_name="tag",
            step_desc="docker tag", steps=steps,
        )

        # Step 4: docker push
        logging.info("[%s] 推送镜像: %s", LOG_PUSH, target_image)
        run_command(
            ["docker", "push", target_image],
            timeout=600, log_prefix=LOG_PUSH, step_name="push",
            step_desc="docker push", steps=steps,
        )

        # Step 5: 清理本地镜像（不报错，只记录）
        try:
            subprocess.run(["docker", "rmi", loaded_image], capture_output=True, timeout=30)
            subprocess.run(["docker", "rmi", target_image], capture_output=True, timeout=30)
            steps.append({
                "step": "cleanup",
                "success": True,
                "message": "本地镜像已清理",
            })
        except Exception as e:
            logging.warning("[%s] 清理本地镜像失败: %s", LOG_PUSH, e)

        # Step 6: 删除 tar 文件
        try:
            os.remove(image_path)
            logging.info("[%s] 已删除 tar 文件: %s", LOG_PUSH, image_path)
            steps.append({
                "step": "cleanup_file",
                "success": True,
                "message": f"临时文件已删除: {os.path.basename(image_path)}",
            })
        except Exception as e:
            logging.warning("[%s] 删除 tar 文件失败: %s", LOG_PUSH, e)

        # 拆分镜像名和版本号
        if ":" in image_name:
            image_base, image_tag = image_name.split(":", 1)
        else:
            image_base = image_name
            image_tag = "latest"

        display_target_image = f"{harbor_addr_remote}/{project}/{image_name}"
        display_harbor_url = build_display_url(agreement, repository, port_str)

        return {
            "code": 200,
            "message": f"镜像推送成功: {display_target_image}",
            "data": {
                "repository": repository,
                "project": project,
                "image_name": image_base,
                "image_tag": image_tag,
                "image_file": image_file,
                "target_image": display_target_image,
                "harbor_url": display_harbor_url,
                "steps": steps,
            }
        }

    except Exception as e:
        safe_error_handler(LOG_PUSH, e, "镜像推送异常")


@router.post("/push_chart")
async def push_chart_to_harbor(
    chart_file: str = Query(..., description="Helm Chart 文件名，如 mysql-14.0.3.tgz")
):
    """
    将本地 Helm Chart 文件（.tgz）推送到本机 Harbor Chart 仓库

    流程：
    1. 在 config.yaml 的 harbor.download 目录下查找 .tgz 文件
    2. 读取 harbor 配置（agreement、port、username、password、template）
    3. 通过 helm push 命令上传 Chart 到 Harbor 的 template 项目（使用回环地址避免证书问题）
    4. 删除临时文件
    """
    download_dir = get_download_dir()
    repository = get_repository_ip()

    # 在 download 目录下查找 chart 文件
    chart_path = os.path.join(download_dir, chart_file)
    if not os.path.exists(chart_path):
        raise HTTPException(
            status_code=404,
            detail=f"Chart 文件不存在: {chart_path}"
        )

    # 读取 Harbor 连接信息
    conn = get_harbor_connection_info(require_auth=True, project_key="template")
    agreement = conn["agreement"]
    username = conn["username"]
    password = conn["password"]
    port_str = conn["port_str"]
    project = conn["project"]
    is_https = conn["is_https"]
    harbor_addr_local = conn["harbor_addr_local"]

    oci_registry = build_oci_registry(harbor_addr_local, project)
    steps = []

    try:
        # Step 1: helm registry login
        logging.info("[%s] Helm 登录 Harbor: %s", LOG_PUSH_CHART, harbor_addr_local)
        login_cmd = [
            "helm", "registry", "login", harbor_addr_local,
            "-u", username,
            "--password-stdin",
        ]
        login_cmd.append("--insecure" if is_https else "--plain-http")
        run_command(
            login_cmd, timeout=30, input_str=password,
            log_prefix=LOG_PUSH_CHART, step_name="helm_login",
            step_desc="helm registry login", steps=steps,
        )

        # Step 2: helm push
        logging.info("[%s] 推送 Chart: %s -> %s", LOG_PUSH_CHART, chart_path, oci_registry)
        push_cmd = ["helm", "push", chart_path, oci_registry]
        push_cmd.append("--insecure-skip-tls-verify" if is_https else "--plain-http")
        run_command(
            push_cmd, timeout=300, log_prefix=LOG_PUSH_CHART,
            step_name="helm_push", step_desc="helm push", steps=steps,
        )

        # Step 3: 删除 tgz 文件
        file_name = os.path.basename(chart_path)
        try:
            os.remove(chart_path)
            logging.info("[%s] 已删除文件: %s", LOG_PUSH_CHART, chart_path)
            steps.append({
                "step": "cleanup",
                "success": True,
                "message": f"临时文件已删除: {file_name}",
            })
        except Exception as e:
            logging.warning("[%s] 删除文件失败: %s", LOG_PUSH_CHART, e)

        # 解析 Chart 信息
        chart_name, chart_version, chart_ref = parse_chart_info(file_name)
        display_harbor_url = build_display_url(agreement, repository, port_str)

        return {
            "code": 200,
            "message": f"Chart 推送成功: {chart_ref}",
            "data": {
                "repository": repository,
                "project": project,
                "chart_file": chart_ref,
                "chart_name": chart_name,
                "chart_version": chart_version,
                "harbor_url": display_harbor_url,
                "oci_registry": f"oci://{repository}{':' + port_str if port_str else ''}/{project}",
                "steps": steps,
            }
        }

    except Exception as e:
        safe_error_handler(LOG_PUSH_CHART, e, "Chart 推送异常")



