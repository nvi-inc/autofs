from setuptools import setup

setup(
    name='autosfs',
    packages=[],
    description='Automatic FS observation',
    version='0.1.0',
    url='https://github.com/nvi-inc/auto-sat',
    author='Mario Berube',
    author_email='mario.berube@nviinc.com',
    key_words=['vlbi', 'vcc', 'FS', 'satellite'],
    install_requires=['vcc @ git+https://github.com/nvi-inc/vcc-client', 'sqlalchemy', 'watchdog', 'pexpect',
                      'ttkwidgets', 'tkcalendar']
)