"""One local test entry, ordered checks, bounded processes and actionable reports."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'tests/output/latest'
BASE = os.environ.get('BAROMETER_URL', 'http://127.0.0.1:8321').rstrip('/')
PYTHON = sys.executable


def api(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(BASE+path, data=body,
                                    headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


class Suite:
    def __init__(self, args):
        self.args = args
        self.results = []
        self.env = dict(os.environ, TEST_OUTPUT_DIR=str(OUTPUT), BAROMETER_URL=BASE,
                        TEST_REQUIRE_LIGHT='0', PYTHONUNBUFFERED='1')
        self.child = None
        self.server = None
        self.server_log = None
        self.original = None
        self.interrupted = False

    def save(self):
        passed = sum(r['status']=='PASS' for r in self.results)
        failed = sum(r['status']=='FAIL' for r in self.results)
        skipped = sum(r['status']=='SKIP' for r in self.results)
        report = dict(mode='unit' if self.args.unit else 'full', url=BASE,
                      passed=passed, failed=failed, skipped=skipped,
                      interrupted=self.interrupted, checks=self.results)
        temp=OUTPUT/'report.json.tmp'
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(OUTPUT/'report.json')
        lines=[f'PASS {passed} / FAIL {failed} / SKIP {skipped}']
        for r in self.results:
            lines.append(f"{r['status']} [{r['name']}] {r['detail']}")
            if r['status']=='FAIL':
                lines.append('  排查: '+r['hint'])
                lines.append('  日志: '+r['log'])
        (OUTPUT/'summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')

    def record(self, name, status, detail, hint='', log='', seconds=0):
        self.results.append(dict(name=name,status=status,detail=detail,hint=hint,
                                 log=str(log),seconds=round(seconds,2)))
        self.save()
        print(f'{status} [{name}] {detail}', flush=True)
        if status=='FAIL':
            print(f'  排查: {hint}\n  日志: {log}', flush=True)

    @staticmethod
    def terminate(child):
        # Subprocesses have their own session; never kill another service/browser.
        try: os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError: return
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait(timeout=5)

    def step(self, name, hint, command=None, function=None, timeout=120):
        log=OUTPUT/(f'{len(self.results)+1:02d}-'+name+'.log')
        start=time.monotonic()
        print(f'RUN  [{name}]',flush=True)
        try:
            if command:
                with log.open('w',encoding='utf-8') as stream:
                    self.child=subprocess.Popen(command,cwd=ROOT,env=self.env,stdout=stream,
                                                stderr=subprocess.STDOUT,start_new_session=True)
                    try: code=self.child.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        self.terminate(self.child)
                        raise RuntimeError(f'超过 {timeout} 秒，已结束本检查的进程')
                    finally:
                        if self.child is not None and self.child.poll() is not None:
                            self.terminate(self.child)  # Reap helpers after failed browser runs.
                            self.child=None
                if code:
                    lines=log.read_text(encoding='utf-8',errors='replace').splitlines()
                    errors=[line.strip() for line in lines if line.startswith(('Error:', 'AssertionError', 'ValueError:', 'RuntimeError:', 'FileNotFoundError:', 'PermissionError:'))]
                    detail=('；'+errors[-1]) if errors else ''
                    raise RuntimeError(f'退出码 {code}{detail}')
                detail='完成'
            else:
                detail=str(function() or '完成')
                log.write_text(detail+'\n',encoding='utf-8')
            self.record(name,'PASS',detail,hint,log,time.monotonic()-start)
            return True
        except (KeyboardInterrupt, SystemExit): raise
        except Exception as error:
            with log.open('a',encoding='utf-8') as stream:
                stream.write('\n'+traceback.format_exc())
            self.record(name,'FAIL',str(error),hint,log,time.monotonic()-start)
            tail=log.read_text(encoding='utf-8',errors='replace').splitlines()[-16:]
            print('\n'.join('  '+line for line in tail),flush=True)
            return False

    def wait_idle(self):
        for _ in range(22):
            light=api('/api/light')['light']
            if not light['owner']: return light
            time.sleep(.5)
        raise RuntimeError('仍有体验会话占用灯光，请停止其他页面后重试；测试不会强制抢占')

    def prepare_service(self):
        url=urllib.parse.urlparse(BASE)
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.path:
            raise ValueError('完整测试请使用板端 http://127.0.0.1:端口，远程浏览器无法代测板端摄像头')
        try: health=api('/api/health')
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError) and getattr(error.reason,'errno',None)!=errno.ECONNREFUSED:
                raise
            self.server_log=(OUTPUT/'server.log').open('w',encoding='utf-8')
            self.server=subprocess.Popen([PYTHON,'-u','server/app.py','--host','127.0.0.1',
                                          '--port',str(url.port or 80)],cwd=ROOT,env=self.env,
                                         stdout=self.server_log,stderr=subprocess.STDOUT,start_new_session=True)
            health=None
        # Refuse an unrelated HTTP service even if it happens to expose /api/health.
        matches=[]
        for process in Path('/proc').iterdir():
            if not process.name.isdigit(): continue
            try:
                argv=(process/'cmdline').read_bytes().decode().split('\0')
                cwd=(process/'cwd').resolve()
                entry=next((v for v in argv if v.endswith('server/app.py')),None)
                if entry and (cwd/entry).resolve()==ROOT/'server/app.py' and '--port' in argv and argv[argv.index('--port')+1]==str(url.port or 80):
                    matches.append(int(process.name))
            except (OSError,ValueError): pass
        if len(matches)!=1:
            raise RuntimeError(f'当前项目服务进程数为 {len(matches)}，拒绝复用或启动重复服务')
        if self.server and matches[0]!=self.server.pid:
            raise RuntimeError('端口被另一进程抢占')
        for _ in range(60):
            if self.server and self.server.poll() is not None:
                raise RuntimeError('服务启动退出，请查看 tests/output/latest/server.log')
            try:
                health=api('/api/health')
                if health['state']=='ready': break
                if health['state'] in ('error','failed'): raise RuntimeError(str(health))
            except urllib.error.URLError: pass
            time.sleep(.5)
        if not health or health['state']!='ready': raise RuntimeError(f'模型未就绪: {health}')
        light=api('/api/light')['light']
        if light['owner']: raise RuntimeError('请先在其他网页停止体验，避免测试接管正在使用的摄像头和灯带')
        self.original=dict(model=health['emotion_key'],layout=light['group_mode'])
        if health['emotion_key']!='hse-b0':api('/api/model',{'model':'hse-b0'})
        return '复用已有服务' if not self.server else '已启动临时服务，结束后自动停止'

    def dependencies(self):
        sys.path.insert(0,str(ROOT/'server'))
        from lighting import load_config
        load_config(ROOT/'configs/light.json')
        for module in ('cv2','numpy','onnxruntime','spacemit_vision'):
            __import__(module)
        for name in ('single','multi','none'):
            if not (ROOT/f'web/test-frames/{name}.jpg').is_file():
                raise FileNotFoundError(f'web/test-frames/{name}.jpg 缺失')
        return '配置、Python 依赖及三张共用测试图片完整'

    def browser_dependencies(self):
        node=shutil.which('node')
        if not node:raise RuntimeError('缺少 Node.js 22+')
        version=subprocess.check_output([node,'--version'],text=True,timeout=5).strip()
        if int(version.lstrip('v').split('.')[0])<22:raise RuntimeError('Node.js 版本过低: '+version)
        chromium=os.environ.get('CHROMIUM','/usr/lib/chromium/chromium')
        if not os.access(chromium,os.X_OK):raise RuntimeError('Chromium 不可执行: '+chromium)
        return version+'; '+chromium

    def controller(self):
        cfg=json.loads((ROOT/'configs/light.json').read_text())
        if not cfg['enabled']:raise RuntimeError('配置已关闭实体输出；连接控制器后启用，或明确使用 --no-hardware')
        port=Path(cfg['port'])
        if not port.exists():raise RuntimeError('控制器串口不存在: '+str(port))
        if not os.access(port,os.R_OK|os.W_OK):raise PermissionError('串口无读写权限: '+str(port))
        for _ in range(10):
            state=api('/api/light')['light']['device']
            if state['status']=='connected':
                assert state['led_count']==cfg['led_count'],state
                return f"USB 已连接，配置 {state['led_count']} 颗；不代表灯带接线或发光已验证"
            time.sleep(.5)
        raise RuntimeError(str(state))

    def restore(self):
        if self.child:
            self.terminate(self.child);self.child=None
        if self.server:
            self.terminate(self.server)
            if self.server_log:self.server_log.close()
            self.server=None
            return '已停止本次启动的临时服务；WLED 实时超时后按自身设置运行'
        if self.original:
            self.wait_idle()
            current=api('/api/health')['emotion_key']
            if current!=self.original['model']:api('/api/model',{'model':self.original['model']})
            api('/api/light/layout',{'mode':self.original['layout'],'run_id':'test-suite-restore'})
            return '已有服务保留运行，原模型与灯光模式已恢复'
        return '未接管服务，无需恢复'

    def run(self):
        try:
            self.step('灯光规则','查看失败用例名；检查确认时序、轨迹、电流对齐或 USB 重连逻辑。',
                      command=[PYTHON,'tests/test_lighting.py'],timeout=40)
            if self.args.unit:return
            deps=self.step('配置与依赖','按 README 安装依赖；核对 configs、模型引擎与 web/test-frames。',function=self.dependencies)
            models=self.step('模型文件校验','运行 .venv/bin/python scripts/download_models.py；校验不符的文件需核对来源后更换。',
                             command=[PYTHON,'scripts/download_models.py','--verify-only'],timeout=90)
            browser=self.step('浏览器依赖','安装 Node.js 22+ 和 Chromium，或通过 CHROMIUM 指定可执行文件。',function=self.browser_dependencies)
            if not (deps and models):
                self.record('服务及联调','SKIP','配置、依赖或模型校验失败，先修复前置条件');return
            if not self.step('服务就绪','查看本项日志和 server.log；检查端口、模型加载及是否有网页正在体验。',function=self.prepare_service):
                self.record('接口与硬件','SKIP','服务未就绪，无法继续联调');return
            if self.args.no_hardware:
                self.record('USB 控制器','SKIP','用户指定 --no-hardware')
            else:
                self.step('USB 控制器','检查 configs/light.json、USB 数据线、dialout 组、WLED 数量/亮度/限流；无需 sudo 运行 Demo。',function=self.controller)
            api_ok=self.step('模型与接口','查看失败图片或断言；确认 B0、请求返回值、模型分类和灯光目标，测试图片不是准确率评测集。',
                             command=[PYTHON,'tests/test_api.py'],timeout=90)
            if browser:
                cases=[('页面布局','visual'),('多人页面','group'),('会话同步','shared')]
                for name,case in cases:
                    if case=='group' and not api_ok:
                        self.record(name,'SKIP','本轮多人图片或 fixture 生成失败，不复用历史输出');continue
                    self.wait_idle()
                    self.step(name,'查看对应日志、截图和浏览器错误；本项使用测试图片，未验证真实摄像头。',
                              command=['node','tests/test_ui.js','--case',case],timeout=150)
                self.wait_idle()
                self.step('模型切换与交互','检查模型切换、人数状态、镜像、权限拒绝和停止释放的失败断言。',
                          command=['node','tests/test_browser_contract.js'],timeout=180)
                # Recover B0 if the preceding model-switch check failed midway.
                self.wait_idle()
                if api('/api/health')['emotion_key']!='hse-b0':api('/api/model',{'model':'hse-b0'})
                self.step('异常恢复','检查取消开启、迟到帧、请求失败及资源释放；此处故障为合成注入。',
                          command=['node','tests/test_browser_contract.js','--lifecycle'],timeout=120)
                if self.args.no_hardware:
                    self.record('真实摄像头','SKIP','用户指定 --no-hardware')
                else:
                    self.wait_idle()
                    self.step('真实摄像头','检查 /dev/video*、扩展坞数据连接、video 权限及其他应用占用；本项要求至少 1280×720。',
                              command=['node','tests/test_camera.js'],timeout=70)
                if self.args.soak:
                    self.wait_idle()
                    self.env['SOAK_SECONDS']=str(self.args.soak)
                    cmd=['node','tests/test_soak.js']+(['--frames'] if self.args.no_hardware else [])
                    self.step('持续压力','检查 soak/report.json 中序号、帧数、错误和 RSS；短程通过不代表长期稳定。',
                              command=cmd,timeout=self.args.soak+80)
                else:self.record('持续压力','SKIP','可用 --soak 1800 增加 30 分钟检查')
            else:self.record('页面与摄像头','SKIP','浏览器依赖不满足')
        except KeyboardInterrupt:
            self.interrupted=True
            self.record('用户中断','FAIL','测试已中断','修复或重新运行；已完成的结果保留。')
        except Exception as error:
            path=OUTPUT/'orchestrator.log';path.write_text(traceback.format_exc(),encoding='utf-8')
            self.record('测试调度','FAIL',str(error),'查看调度日志，检查是否出现了新的会话占用或服务退出。',path)
        finally:
            self.step('现场恢复','检查现有服务、模型和灯光模式；不要重复启动占用相同端口的服务。',function=self.restore)


def main():
    parser=argparse.ArgumentParser(description='完整验证 Demo 并输出逐项结果。运行前在其他页面停止体验。')
    parser.add_argument('--unit',action='store_true',help='仅运行离线灯光规则测试，不启动服务或接触硬件')
    parser.add_argument('--no-hardware',action='store_true',help='明确跳过 USB 控制器和真实摄像头检查')
    parser.add_argument('--soak',type=int,metavar='SECONDS',help='追加持续测试，至少 10 秒，建议 1800 秒')
    args=parser.parse_args()
    if args.soak is not None and args.soak<10:parser.error('--soak 至少 10 秒')
    if args.unit and (args.no_hardware or args.soak is not None):parser.error('--unit 不与其他测试模式组合')
    os.chdir(ROOT)
    parent=OUTPUT.parent;parent.mkdir(parents=True,exist_ok=True)
    with (parent/'.suite.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('FAIL 另一个 run_test.sh 正在运行，拒绝同时争用设备。',file=sys.stderr);return 2
        if OUTPUT.is_symlink():raise RuntimeError('拒绝清理符号链接输出目录')
        if OUTPUT.exists():shutil.rmtree(OUTPUT)
        OUTPUT.mkdir()
        signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        suite=Suite(args);suite.run()
        print('\n'+(OUTPUT/'summary.txt').read_text(encoding='utf-8'))
        print('完整报告: '+str(OUTPUT/'report.json'))
        return 130 if suite.interrupted else 1 if any(r['status']=='FAIL' for r in suite.results) else 0


if __name__=='__main__':raise SystemExit(main())
