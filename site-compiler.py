#!/usr/bin/env python3
import os
import sys
import shutil
import time
import signal
import fnmatch
from functools import partial
from multiprocessing import Process
from pathlib import Path
import argparse
from hashlib import md5
import socketserver
import http.server
import logging
import subprocess
import inspect
from abc import ABC
import re
import inspect
from functools import wraps

from markdown import markdown
from jinja2 import Template, FileSystemLoader, Environment
from yaml import load, dump, load_all
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

# courtesy of Nadi Alramli via SO, Thanks! https://stackoverflow.com/questions/1389180/automatically-initialize-instance-variables
# updated to use python3 getfullargspec
def initializer(func):
    """
    Automatically assigns the parameters to the class of the function it wraps
    """
    fullspec = inspect.getfullargspec(func)
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        for name, arg in list(zip(fullspec.args[1:], args)) + list(kwargs.items()): # starting the zip at idx 1 excludes `self` and then we just grab the kwargs
            setattr(self, name, arg)
        if fullspec.defaults:
            for name, default in zip(reversed(fullspec.args), reversed(fullspec.defaults)):
                if not hasattr(self, name):
                    setattr(self, name, default)
        func(self, *args, *kwargs)
    return wrapper

def server(port, directory):
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    with socketserver.TCPServer(("", port), handler) as httpd:
        logging.getLogger("Server").info(f"serving at port {port}")
        httpd.serve_forever()

class DirectoryWatcher():
    def __init__(self, directory, ignore_patterns=None, init=True):
        self.directory = Path(directory)
        self.path_hash = dict()
        self.ignore_patterns = ignore_patterns
        self.logger = logging.getLogger(f"DirectoryWatcher")
        if init:
            self.dirty()

    def dirty(self):
        dirty = False
        for path in self.directory.glob("**/*"):
            if self.ignore_patterns:
                skip = False
                for pattern in self.ignore_patterns:
                    if fnmatch.fnmatch(str(path), pattern):
                        skip = True
                        break
                if skip:
                    self.logger.debug(f"Skipping {path}")
                    continue
            try:
                with path.open("rb") as f:
                    data = f.read()
            except PermissionError as pemerr:
                continue
            h = md5(data).hexdigest()
            name = str(path.absolute())
            dirty = dirty or name not in self.path_hash or self.path_hash[name] != h
            self.path_hash[name] = h
        return dirty


class Post:
    def __init__(self, source_text, front_matter, body_text, metadata, rendered_text):
        self.source_text = source_text
        self.front_matter = front_matter
        self.body_text = body_text
        self.metadata = metadata
        self.rendered_text = rendered_text
        self.html = ""

def serialize_post(source_text):
    yaml_docs = source_text.split("---")
    if len(yaml_docs)>2:
        front_matter = yaml_docs[1]
        body_text = "".join(yaml_docs[2:])
    else:
        front_matter = None
        body_text = source_text
    try:
        metadata = next(load_all(source_text, Loader=Loader))
    except Exception as e:
        metadata = None
        logging.getLogger("main").error(e)
    return Post(source_text, front_matter, body_text, metadata, "")


#TODO (owen) DOC: This can be imported by the user via
#`from __main__ import UserExtension` anything from this file can be imported in this way
# WARNING: this is kind of a dangerous pattern because it means any kind of user code can be run, so don't use
# site-compiler to compile strange websites that you didn't write
class UserExtension(ABC):
    """
    A user definied extension to the site-compiler
    """
    def __init__(self, logger, working_dir, out_dir, site_data, jinja_env):
        pass

    def forEachPost(self, name, post):
        pass

    def finalize(self):
        pass


class Main():
    def __init__(self, args):
        self.logger = logging.getLogger("main")
        self.args = args
        if args.path and os.path.exists(args.path):
            self.working_directory = Path(os.path.abspath(args.path))
        else:
            self.working_directory = Path(os.path.abspath(os.getcwd()))
        if not self.working_directory.exists():
            self.logger.error(f"Given path: {self.working_directory} does not exist!")
            raise Exception("Bad main working directory!")
        self.app_data = self.working_directory/ ".site-compiler" # TODO (owen): put this in AppData on windows and where ever is the equal on unix
        if not self.app_data.exists():
            self.app_data.mkdir(parents=True)
        self.out_dir = Path(os.path.abspath(args.output_dir))
        self.site_conf = self.working_directory / "site.yaml"
        self.templates_dir = self.working_directory / "templates"
        self.posts_dir = self.working_directory / "posts"
        if self.args.drafts:
            self.drafts_dir = self.working_directory / "drafts"
        else:
            self.drafts_dir = None
        if not self.templates_dir.exists():
            self.logger.error("Can't work without templates")
            sys.exit(-1)
        self.jinja_env = Environment(loader=FileSystemLoader([str(self.templates_dir), str(self.posts_dir), str(self.working_directory)]))
        if self.site_conf.exists():
            with self.site_conf.open() as infstream:
                self.site_data = load(infstream, Loader=Loader)
        if self.site_data and "ignore-patterns" in self.site_data:
            self.ignore_patterns = self.site_data["ignore-patterns"]
        else:
            self.ignore_patterns = []
        assert(self.templates_dir.exists() and self.templates_dir.is_dir())
        self.load_user_extensions()
        #assert(self.posts_dir.exists() and self.posts_dir.is_dir()) # NOTE (owen): no need to assert that post exist because this need not be used to compile a blog

    def run(self):
        if not os.path.exists(args.path):
            self.logger.error(f"{args.path} does not exist")
            sys.exit(-1)
        self.compile()
        if self.args.serve or self.args.watch:
            server_process = Process(target=server, args=(self.args.port, self.args.output_dir))
            if self.args.serve:
                server_process.start()
            self.dir_watcher = DirectoryWatcher(os.path.abspath(self.args.path), ignore_patterns=self.ignore_patterns)
            quit = False
            starttime = time.time()
            every = 1
            def sig_int(sig, frame):
                nonlocal quit
                quit = True
            def sig_term(sig, frame):
                nonlocal quit
                quit = True
            signal.signal(signal.SIGINT, sig_int)
            signal.signal(signal.SIGTERM, sig_term)
            while not quit:
                if(self.args.watch):
                    deltatime = time.time() - starttime
                    if deltatime > every:
                        if self.dir_watcher.dirty():
                            self.compile()
                        starttime = time.time()
            if self.args.serve:
                server_process.terminate()
        self.logger.info("bye bye!")

    def compile(self):
        self.initialize_user_extensions()
        templates_dict = {}
        posts_dict = {}
        def read_file(f, dic, root=None, serializer = lambda d: d):
            with f.open() as inf:
                if root:
                    name = str(f.relative_to(root))
                else:
                    name = str(f.absolue())
                dic[name] = serializer(inf.read())
        def read_dir(d, dic, root=None, file_ext=None, serializer = lambda d: d):
            assert(d.is_dir())
            exclude_paths = []
            for pattern in self.ignore_patterns:
                exclude_paths.extend(d.rglob(pattern))
            for f in d.iterdir():
                if f in exclude_paths:
                    continue
                if f.is_file():
                    if file_ext is None:
                        read_file(f, dic, root, serializer=serializer)
                    elif f.name.endswith(file_ext):
                        read_file(f, dic, root, serializer=serializer)
                else:
                    read_dir(f, dic, file_ext = file_ext, serializer=serializer)
        read_dir(self.templates_dir, templates_dict, root=self.templates_dir)
        if(self.posts_dir.exists()):
            read_dir(self.posts_dir, posts_dict, root=self.posts_dir, file_ext=".md", serializer=serialize_post)
        if self.args.drafts:
            read_dir(self.drafts_dir, posts_dict, root=self.drafts_dir, file_ext=".md", serializer=serialize_post)
        for name, post in posts_dict.items():
            self.logger.info(f"Rendering post {name}")
            post_metadata = post.metadata
            template = self.jinja_env.from_string(post.body_text)
            if post.metadata:
                post.rendered_text = template.render(site=self.site_data, **post.metadata)
            else:
                post.rendered_text = template.render(site=self.site_data)
            markdown_extensions = []
            markdown_extensions_configurations = {}
            if self.site_data and "markdown-extensions" in self.site_data:
                markdown_extensions.extend(self.site_data["markdown-extensions"])
            if self.site_data and "markdown-extensions-configurations" in self.site_data:
                markdown_extensions_configurations.update(**self.site_data["markdown-extensions-configurations"])
            if post.metadata and "markdown-extensions" in post.metadata:
                markdown_extensions.extend(post.metadata["markdown-extensions"])
            if post.metadata and "markdown-extensions-configurations" in post.metadata:
                markdown_extensions_configurations.update(**post.metadata["markdown-extensions-configurations"])
            post.html = markdown(post.rendered_text, extensions=markdown_extensions, extensions_configs=markdown_extensions_configurations)
            post.name = name
            if "title" in post.metadata:
                post.toc = post.metadata["title"].replace(" ", "-")
            else:
                post.toc = post.name.strip(".md").replace(" ", "-")
            # note: this makes using the metadata easier from templates
            for key, value in post.metadata.items():
                setattr(post, key, value)
            # run user extensions on each post
            for extension in self.user_extension_instances:
                extension.forEachPost(name, post)
        for name, template in templates_dict.items():
            self.logger.info(f"Rendering template {name}")
            template = self.jinja_env.get_template(name)
            rendered = template.render(site=self.site_data, posts=list(posts_dict.values()))
            if not self.out_dir.exists():
                self.out_dir.mkdir(parents=True)
            out = self.out_dir/name
            self.logger.info(f"Writing rendered template to {out}")
            with out.open("w", encoding="utf-8") as outf:
                outf.write(rendered)
        if self.site_data and "copy-paths" in self.site_data:
            copy_path_names = self.site_data["copy-paths"]
            assert(type(copy_path_names) is list)
            for path_name in copy_path_names:
                src_path = self.site_conf.parent / path_name
                dst_path = self.out_dir / path_name
                if src_path.is_dir():
                    self.logger.info(f"Copying {src_path}{os.path.sep} to {dst_path}{os.path.sep}")
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*self.ignore_patterns))
                else:
                    ignore=False
                    for ignore_pattern in self.ignore_patterns:
                        if fnmatch.fnmatch(path_name, ignore_pattern):
                            ignore=True
                            self.logger.info(f"Ignoring {path_name}")
                            break
                    if ignore:
                        continue
                    else:
                        self.logger.info(f"Copying {src_path} to {dst_path}")
                        shutil.copyfile(src_path, dst_path)
        for extension in self.user_extension_instances:
            extension.finalize()

    def load_user_extensions(self):
        """
        NOTES:
        2 user extensions on the same site cannot have conflicting site-package requirements
        """
        # path is path to top level user folder (i.e. the top level site folder)
        working_dir = Path(self.working_directory)
        assert(working_dir.exists())
        requirements_path = working_dir / "requirements.txt"
        venv_path = self.app_data / ".venv"
        lib_path = venv_path / "Lib" / "site-packages" if sys.platform == "win32" else venv_path / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        if requirements_path.exists():
            do_install = False
            self.logger.info(f"Found extension-requirements at {requirements_path}")
            # check to see if site local venv exists and if requirements already installed
            if venv_path.exists():
                self.logger.info("Existing venv found")
                if lib_path.exists():
                    self.logger.info("Existing site-packages found")
                    sys.path.append(str(lib_path))
                    import pkg_resources # late import insures that the site local venv site-packages are read by pkg_resources
                    with requirements_path.open("r") as inf:
                        requires = [str(r) for r in pkg_resources.parse_requirements(inf.read())]
                    try:
                        pkg_resources.require(requires) # throws if requirements met in current path
                        self.logger.info("All requirements satisfied. Skipping installation")
                        do_install = False # redundant but informative
                    except (pkg_resources.DistributionNotFound, pkg_resources.VersionConflict) as err:
                        reason = re.sub(r'((?<=[a-z])[A-Z]|(?<!\A)[A-Z](?=[a-z]))', r' \1', err.__class__.__name__)
                        self.logger.info(f"Requirement {err.req} not met. Reason: \"{reason}\"")
                        do_install = True
                    # TODO (owen): there are 2 more possible exceptions "UnkownExtra" and "ExtractionError" I've never seen these in common practace and don't know what they mean but I should probably handle them here
                else:
                    self.logger.info("No site packages found in existing venv")
                    do_install = True
            else:
                # make a new venv in the user folder
                self.logger.info(f"Making new venv for extension-requirements at {venv_path}")
                subprocess.check_call([sys.executable, "-m", "venv", str(venv_path)])
                do_install = True
            local_python = venv_path / "Scripts" / "python.exe" if sys.platform == "win32" else venv_path / "bin" / "python"
            assert(local_python.exists())
            if do_install:
                # install user extension requirements to site local virtualenv
                self.logger.info(f"Installing extension requirements to site venv")
                cmd = [str(local_python), "-m", "pip", "install", "-r", str(requirements_path)]
                self.logger.info(f"Running subprocess: {' '.join(cmd)}")
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                    for c in iter(proc.stdout.readline,  b''):
                        print(c.decode("utf-8"))
                    proc.communicate()
            # append the user site-packages to the current executable path (this is needed to successfully import user extension module)
            assert(lib_path.exists())
            sys.path.append(str(lib_path))
        sys.path.append(str(working_dir)) # add user folder to system path
        # TODO (owen) DOCS: Document that the extension module should me named "extensions" and that it can be any python importable, i.e. package or module
        import extensions # initial import loads the "extensions" module cache entry used on the next line
        # use inspect to get all classes that subclass UserExtension
        self.user_extension_classes = [cls for name, cls in inspect.getmembers(sys.modules["extensions"]) if inspect.isclass(cls) and issubclass(cls, (UserExtension))]

    def initialize_user_extensions(self):
        # initialize instance list with list of fresh instaces
        self.user_extension_instances = [e(logging.getLogger(f"{e.__name__}"), self.working_directory, self.out_dir, self.site_data, self.jinja_env) for e in self.user_extension_classes]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compiles a static site from markdown files and templates")
    parser.add_argument("path", default=None)
    parser.add_argument("-o", "--output-dir", default="_site")
    parser.add_argument("-d", "--drafts", action="store_true")
    parser.add_argument("-w", "--watch", action="store_true")
    parser.add_argument("-s", "--serve", action="store_true")
    parser.add_argument("-p", "--port", type=int, default=8000)
    args = parser.parse_args()
    main = Main(args)
    main.run()
