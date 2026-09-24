"""IP 纯净度检查插件包

每个源一个模块，地位平等：模块暴露 `SOURCE`（IppureSourceSpec 元数据）与
`create_checker(config)` 工厂；被选中的插件由组合根构造（见 ippure.toml）。
"""
